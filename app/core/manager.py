from __future__ import annotations

import csv
import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from typing import Callable, Deque, Dict, List, Optional

from .engine import DownloadEngine, target_path, probe_length
from .models import AppSettings, ProgressEvent, TaskStatus, TrackTask
from .queue_store import read_queue, write_queue

EventSink = Callable[[ProgressEvent], None]


class DownloadManager:
    def __init__(self, settings: AppSettings, sink: Optional[EventSink] = None):
        self.settings = settings
        self.sink = sink or (lambda e: None)
        self.tasks: List[TrackTask] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pause = threading.Event()  # set = paused
        self._wake = threading.Event()
        self._threads: List[threading.Thread] = []
        self._running = False
        self._priority_uid: Optional[str] = None
        self._single_only_uid: Optional[str] = None
        self._last_progress_ts: float = time.time()
        self._last_success_ts: float = 0.0
        self._done_count = 0
        self._total_count = 0
        self._engine: Optional[DownloadEngine] = None
        self._results_path = ""
        self._stall_monitor_thread: Optional[threading.Thread] = None

    # ---- events ----
    def emit(self, ev: ProgressEvent):
        try:
            self.sink(ev)
        except Exception:
            pass

    def log(self, msg: str):
        self.emit(ProgressEvent(kind="log", message=msg))

    # ---- queue mgmt ----
    def load_queue(self, path: Optional[str] = None):
        p = path or self.settings.queue_file
        with self._lock:
            self.tasks = read_queue(p)
            for t in self.tasks:
                t.status = TaskStatus.PENDING
                t.progress = 0.0
                t.phase = ""
            self._total_count = len(self.tasks)
            self._done_count = 0
        self.emit(ProgressEvent(kind="overall", overall_done=0, overall_total=self._total_count, message="очередь загружена"))

    def save_queue(self, path: Optional[str] = None):
        p = path or self.settings.queue_file
        with self._lock:
            write_queue(p, self.tasks)

    def prescan_existing(self, progress_cb=None):
        """Scan queue for already downloaded files and mark as SKIPPED. Returns count skipped."""
        settings = self.settings
        out_dir = settings.output_dir
        tf = (settings.target_format or "mp3").lower()
        total = 0
        skipped = 0
        with self._lock:
            tasks = list(self.tasks)
        total = len(tasks)
        for i, t in enumerate(tasks, 1):
            if self._stop.is_set():
                break
            if t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.CANCELED):
                continue
            try:
                fp = target_path(out_dir, settings.filename_template, t.artist, t.title, t.album, tf)
                if settings.skip_existing and os.path.exists(fp) and os.path.getsize(fp) > 50_000:
                    length, _ = probe_length(fp)
                    t.status = TaskStatus.SKIPPED
                    t.out_file = fp
                    t.note = "уже есть (пропуск)"
                    t.phase = "готово"
                    t.progress = 1.0
                    t.dur_delta_s = None if not (length and t.seconds) else length - t.seconds
                    skipped += 1
                    self.emit(ProgressEvent(kind="track", task_uid=t.uid, status=t.status.value, message=t.note, data={"phase": t.phase, "progress": 1.0, "note": t.note, "out_file": fp}))
            except Exception:
                pass
            if progress_cb and (i % 25 == 0 or i == total):
                try:
                    progress_cb(i, total, skipped)
                except Exception:
                    pass
        with self._lock:
            self._done_count = sum(1 for t in self.tasks if t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED))
            done = self._done_count
            tot = self._total_count
        self.emit(ProgressEvent(kind="overall", overall_done=done, overall_total=tot, message=f"сканирование завершено: пропущено {skipped}"))
        self.emit(ProgressEvent(kind="scan_done", message=str(skipped), data={"skipped": skipped, "total": total}))
        return skipped

    def add_task(self, task: TrackTask, to_front: bool = False):
        with self._lock:
            if to_front:
                self.tasks.insert(0, task)
            else:
                self.tasks.append(task)
            self._total_count = len(self.tasks)
        self.save_queue()
        self.emit(ProgressEvent(kind="overall", overall_done=self._done_count, overall_total=self._total_count, message="добавлен трек"))

    # ---- control ----
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    @property
    def last_success_ts(self) -> float:
        # timestamp of last actually downloaded track (OK), 0 if none yet
        return float(self._last_success_ts or 0.0)

    def start(self, single_uid: Optional[str] = None, priority_uid: Optional[str] = None):
        if self._running:
            return
        self._stop.clear()
        self._pause.clear()
        self._single_only_uid = single_uid
        self._priority_uid = priority_uid
        self._running = True
        self._last_progress_ts = time.time()
        self._last_success_ts = 0.0
        self._results_path = os.path.join(self.settings.output_dir, "_jobs", "results.jsonl")
        os.makedirs(os.path.dirname(self._results_path), exist_ok=True)
        self._engine = DownloadEngine(self.settings, progress_cb=self._on_engine_progress)
        n = max(1, min(int(self.settings.workers or 1), 6))
        self._threads = [threading.Thread(target=self._worker_loop, args=(i,), daemon=True) for i in range(n)]
        for t in self._threads:
            t.start()
        self._stall_monitor_thread = threading.Thread(target=self._stall_monitor, daemon=True)
        self._stall_monitor_thread.start()
        self.emit(ProgressEvent(kind="workers", message=str(n), data={"count": n}))
        self.log(f"запуск: воркеров {n}, очередь {len(self.tasks)}")

    def pause(self):
        self._pause.set()
        self.log("пауза")

    def resume(self):
        self._pause.clear()
        self._wake.set()
        self.log("продолжение")

    def stop(self):
        self._stop.set()
        self._pause.clear()
        self._wake.set()
        if self._engine:
            self._engine.cancel_event.set()
        self.log("остановка...")

    def request_priority(self, uid: str, single_only: bool = False):
        with self._lock:
            self._priority_uid = uid
            self._single_only_uid = uid if single_only else None
        self._wake.set()
        self.log(f"приоритет для {uid}" + (" (только он)" if single_only else ""))

    # ---- internals ----
    def _next_task(self) -> Optional[TrackTask]:
        with self._lock:
            # priority/single
            if self._single_only_uid:
                for t in self.tasks:
                    if t.uid == self._single_only_uid and t.status == TaskStatus.PENDING:
                        t.status = TaskStatus.QUEUED
                        return t
                return None
            if self._priority_uid:
                for t in self.tasks:
                    if t.uid == self._priority_uid and t.status == TaskStatus.PENDING:
                        t.status = TaskStatus.QUEUED
                        self._priority_uid = None
                        return t
                self._priority_uid = None
            for t in self.tasks:
                if t.status == TaskStatus.PENDING:
                    t.status = TaskStatus.QUEUED
                    return t
        return None

    def _mark_progress(self, success: bool = False):
        self._last_progress_ts = time.time()
        if success:
            self._last_success_ts = time.time()

    def _worker_loop(self, wid: int):
        try:
            sys_enc(wid)
        except Exception:
            pass
        while not self._stop.is_set():
            while self._pause.is_set() and not self._stop.is_set():
                self._wake.wait(0.5)
                self._wake.clear()
            if self._stop.is_set():
                break
            task = self._next_task()
            if task is None:
                # if single-only and it's done, or no more tasks
                if self._single_only_uid:
                    break
                # check if all terminal
                with self._lock:
                    if all(t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED) for t in self.tasks):
                        break
                time.sleep(0.4)
                continue
            self.emit(ProgressEvent(kind="track", task_uid=task.uid, status=task.status.value, message=f"старт: {task.display_name}", data={"phase": "старт", "wid": wid, "progress": 0.0}))
            try:
                result = self._engine.process_task(task, worker_id=wid)
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.note = f"крит: {e}"
                task.phase = "ошибка"
                task.progress = 1.0
                result = task
            # if engine didn't emit final with wid, ensure UI gets state
            try:
                self.emit(ProgressEvent(kind="track", task_uid=task.uid, status=result.status.value, message=result.phase or result.note or "", data={"phase": result.phase or "", "wid": wid, "progress": result.progress, "note": result.note}))
            except Exception:
                pass
            self._record_result(result)
            # "last successful download" means actually downloaded (OK), not skipped/attempt
            self._mark_progress(success=(result.status == TaskStatus.OK))
            with self._lock:
                # copy back fields
                for t in self.tasks:
                    if t.uid == result.uid:
                        t.status = result.status
                        t.source = result.source
                        t.quality_label = result.quality_label
                        t.match = result.match
                        t.dur_delta_s = result.dur_delta_s
                        t.out_file = result.out_file
                        t.note = result.note
                        t.phase = result.phase
                        t.progress = result.progress
                        break
                self._done_count = sum(1 for t in self.tasks if t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED))
                done = self._done_count
                total = self._total_count
            self.emit(ProgressEvent(kind="overall", overall_done=done, overall_total=total, message=f"{done}/{total}"))
        # end of this worker; mark idle
        try:
            self.emit(ProgressEvent(kind="worker_idle", message=str(wid), data={"wid": wid}))
        except Exception:
            pass
        # if no threads alive, mark stopped
        alive = any(t.is_alive() for t in self._threads if t is not threading.current_thread())
        if not alive:
            with self._lock:
                self._running = False
            self.emit(ProgressEvent(kind="overall", overall_done=self._done_count, overall_total=self._total_count, message="завершено/остановлено"))
            self.emit(ProgressEvent(kind="workers", message="0", data={"count": 0}))

    def _on_engine_progress(self, kind: str, payload: dict):
        uid = payload.get("uid")
        if kind == "track":
            self._mark_progress(success=(payload.get("status") == TaskStatus.OK.value))
            self.emit(ProgressEvent(kind="track", task_uid=uid, status=payload.get("status"), message=payload.get("phase") or payload.get("note") or "", data=payload))
        elif kind == "candidates":
            self.emit(ProgressEvent(kind="log", task_uid=uid, message=f"выбор: {payload.get('best_source')} {payload.get('best')} match={payload.get('best_match')} (кандидатов {payload.get('count')})", data=payload))
        else:
            self.emit(ProgressEvent(kind="log", task_uid=uid, message=str(payload), data=payload))

    def _record_result(self, task: TrackTask):
        try:
            os.makedirs(os.path.dirname(self._results_path), exist_ok=True)
            rec = {
                "uid": task.uid, "artist": task.artist, "title": task.title, "album": task.album, "secs": task.seconds,
                "status": task.status.value, "source": task.source, "qlabel": task.quality_label,
                "match": task.match, "dur_delta_s": task.dur_delta_s, "out_file": task.out_file, "note": task.note,
                "ts": time.time(),
            }
            with open(self._results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _stall_monitor(self):
        # stall = no progress events for N minutes; request engine restart logic handled by manager by stopping/restarting workers
        while self._running and not self._stop.is_set():
            time.sleep(10)
            mins = int(self.settings.stall_restart_minutes or 0)
            if mins <= 0:
                continue
            idle = time.time() - self._last_progress_ts
            if idle >= mins * 60 and self._running and not self._pause.is_set():
                self.log(f"нет прогресса {int(idle)}с >= {mins} мин — авто-перезапуск загрузчика")
                self.emit(ProgressEvent(kind="stall", message=f"перезапуск после простоя {int(idle)}с"))
                self._restart_workers()

    def _restart_workers(self):
        try:
            if self._engine:
                self._engine.cancel_event.set()
            # let workers cycle
            old_engine = self._engine
            self._engine = DownloadEngine(self.settings, progress_cb=self._on_engine_progress)
            self._last_progress_ts = time.time()
            # reset queued-inflight back to pending
            with self._lock:
                for t in self.tasks:
                    if t.status == TaskStatus.QUEUED:
                        t.status = TaskStatus.PENDING
            self.log("движок перезапущен, продолжение очереди")
        except Exception as e:
            self.log(f"ошибка перезапуска: {e}")


def sys_enc(wid=0):
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
