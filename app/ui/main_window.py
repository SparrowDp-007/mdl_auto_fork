from __future__ import annotations

import csv
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..core.config import load_settings, save_settings
from ..core.manager import DownloadManager
from ..core.models import AppSettings, ProgressEvent, TaskStatus, TrackTask, norm_text
from ..core.queue_store import append_tasks, dedupe_tasks, ensure_queue_file, import_csv, read_queue, write_queue

README_TEXT = (
    "Формат CSV (UTF-8): колонки Artist,Title,Album,Seconds\n"
    "Artist - исполнитель (можно пусто, но менее точно)\n"
    "Title  - название трека\n"
    "Album  - альбом (необязательно)\n"
    "Seconds- длительность в секундах (необязательно, повышает точность)\n\n"
    "Пример:\n"
    "Artist,Title,Album,Seconds\n"
    "Gorillaz,O Green World,Demon Days,275\n"
    ",Shikanoko Nokonoko Koshitantan,,99\n\n"
    "Поиск идёт по Artist+Title, выбор версии — по совпадению и длительности."
)

STALL_OPTIONS = {
    "Никогда": 0,
    "5 минут": 5,
    "10 минут": 10,
    "20 минут": 20,
    "30 минут": 30,
    "60 минут": 60,
}

FORMAT_OPTIONS = ["mp3", "m4a", "flac", "opus", "wav"]
QUALITY_OPTIONS = {"Лучшее (lossless→цель/320)": "best", "Высокое (320 kbps)": "high", "Среднее (192 kbps)": "medium", "Низкое (128 kbps)": "low", "Своё": "custom"}
ALL_SOURCES = ["NeteaseMusicClient", "KuwoMusicClient", "BilibiliMusicClient", "YouTubeMusicClient"]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.title("MusicDL Fork")
        self.geometry("1480x860")
        self.minsize(1280, 720)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.ui_queue = queue.Queue()
        self.manager = DownloadManager(self.settings, sink=self._on_manager_event)
        self._last_success_ts: float = 0.0
        self._last_success_name: str = ""

        self._build_ui()
        self._load_initial_queue()
        self.after(200, self._poll_ui_queue)
        self.after(1000, self._tick_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- UI build
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # top controls
        top = ctk.CTkFrame(self, corner_radius=10)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        top.grid_columnconfigure(1, weight=1)

        # output folder
        ctk.CTkLabel(top, text="Папка сохранения:").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.out_var = ctk.StringVar(value=self.settings.output_dir)
        ctk.CTkEntry(top, textvariable=self.out_var, width=420).grid(row=0, column=1, padx=4, pady=6, sticky="ew")
        ctk.CTkButton(top, text="Выбрать...", width=100, command=self._choose_out).grid(row=0, column=2, padx=4, pady=6)
        ctk.CTkButton(top, text="Открыть папку", width=120, command=self._open_out_folder).grid(row=0, column=3, padx=4, pady=6)

        # format/quality
        ctk.CTkLabel(top, text="Формат:").grid(row=0, column=4, padx=(12,4), pady=6)
        self.format_var = ctk.StringVar(value=self.settings.target_format if self.settings.target_format in FORMAT_OPTIONS else "mp3")
        ctk.CTkOptionMenu(top, values=FORMAT_OPTIONS, variable=self.format_var, width=90).grid(row=0, column=5, padx=4, pady=6)
        ctk.CTkLabel(top, text="Качество:").grid(row=0, column=6, padx=(12,4), pady=6)
        q_rev = {v:k for k,v in QUALITY_OPTIONS.items()}
        self.quality_var = ctk.StringVar(value=q_rev.get(self.settings.quality_mode, "Высокое (320 kbps)"))
        ctk.CTkOptionMenu(top, values=list(QUALITY_OPTIONS.keys()), variable=self.quality_var, width=220).grid(row=0, column=7, padx=4, pady=6)

        # row1: queue file + stall + sources
        ctk.CTkLabel(top, text="Файл очереди (активный):").grid(row=1, column=0, padx=8, pady=6, sticky="w")
        self.queue_var = ctk.StringVar(value=self.settings.queue_file)
        ctk.CTkEntry(top, textvariable=self.queue_var, state="readonly").grid(row=1, column=1, padx=4, pady=6, sticky="ew")
        ctk.CTkButton(top, text="Выбрать...", width=110, command=self._choose_queue_file).grid(row=1, column=2, padx=4, pady=6)
        ctk.CTkButton(top, text="Формат (README)", width=140, command=self._show_readme).grid(row=1, column=3, padx=4, pady=6)

        ctk.CTkLabel(top, text="Рестарт при простое:").grid(row=1, column=4, padx=(12,4), pady=6)
        stall_rev = {v:k for k,v in STALL_OPTIONS.items()}
        self.stall_var = ctk.StringVar(value=stall_rev.get(int(self.settings.stall_restart_minutes), "20 минут"))
        ctk.CTkOptionMenu(top, values=list(STALL_OPTIONS.keys()), variable=self.stall_var, width=140).grid(row=1, column=5, padx=4, pady=6)
        ctk.CTkButton(top, text="Источники...", width=110, command=self._sources_dialog).grid(row=1, column=6, padx=4, pady=6)
        ctk.CTkButton(top, text="Настройки", width=100, command=self._settings_dialog).grid(row=1, column=7, padx=4, pady=6)

        # main split
        mid = ctk.CTkFrame(self, corner_radius=10)
        mid.grid(row=1, column=0, sticky="nsew", padx=10, pady=6)
        mid.grid_columnconfigure(0, weight=3)
        mid.grid_columnconfigure(1, weight=2)
        mid.grid_rowconfigure(1, weight=1)

        # left: queue + progress
        left = ctk.CTkFrame(mid)
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(6,4), pady=6)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)

        prog = ctk.CTkFrame(left)
        prog.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        prog.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(prog, text="Общий прогресс:").grid(row=0, column=0, padx=6, pady=(6,2), sticky="w")
        self.overall_bar = ctk.CTkProgressBar(prog)
        self.overall_bar.grid(row=0, column=1, padx=6, pady=(6,2), sticky="ew")
        self.overall_bar.set(0)
        self.overall_label = ctk.CTkLabel(prog, text="0 / 0")
        self.overall_label.grid(row=0, column=2, padx=6, pady=(6,2))

        self.workers_frame = ctk.CTkFrame(prog, fg_color="transparent")
        self.workers_frame.grid(row=1, column=0, columnspan=3, padx=6, pady=(2,6), sticky="ew")
        self.workers_frame.grid_columnconfigure(1, weight=1)
        self.worker_labels = {}
        self.worker_bars = {}
        self.worker_pcts = {}
        self._ensure_worker_labels(1)

        # queue actions
        qa = ctk.CTkFrame(left)
        qa.grid(row=1, column=0, sticky="ew", padx=6, pady=4)
        ctk.CTkButton(qa, text="Старт", width=90, fg_color="#2d7d46", hover_color="#25663a", command=self._start).pack(side="left", padx=3, pady=4)
        ctk.CTkButton(qa, text="Пауза", width=90, command=self._pause).pack(side="left", padx=3, pady=4)
        ctk.CTkButton(qa, text="Продолжить", width=100, command=self._resume).pack(side="left", padx=3, pady=4)
        ctk.CTkButton(qa, text="Стоп", width=90, fg_color="#8b2e2e", hover_color="#6f2424", command=self._stop).pack(side="left", padx=3, pady=4)
        ctk.CTkButton(qa, text="Повторить failed/miss", width=170, command=self._retry_failed).pack(side="left", padx=8, pady=4)
        ctk.CTkButton(qa, text="Экспорт отчёта/M3U", width=160, command=self._export_report).pack(side="left", padx=3, pady=4)

        # queue list (lightweight text view)
        qf = ctk.CTkFrame(left)
        qf.grid(row=2, column=0, sticky="nsew", padx=6, pady=6)
        qf.grid_rowconfigure(1, weight=1)
        qf.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(qf, text="Очередь (активные сверху)", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w", padx=6, pady=(6,2))
        self.queue_text = ctk.CTkTextbox(qf, wrap="none")
        self.queue_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        try:
            self.queue_text.configure(state="disabled")
        except Exception:
            pass

        # right: add/find/log/info
        right = ctk.CTkFrame(mid)
        right.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(4,6), pady=6)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)

        addf = ctk.CTkFrame(right)
        addf.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        addf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(addf, text="Добавить трек в очередь", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=2, padx=6, pady=(6,4), sticky="w")
        self.e_artist = ctk.CTkEntry(addf, placeholder_text="Artist")
        self.e_title = ctk.CTkEntry(addf, placeholder_text="Title")
        self.e_album = ctk.CTkEntry(addf, placeholder_text="Album")
        self.e_secs = ctk.CTkEntry(addf, placeholder_text="Seconds", width=100)
        ctk.CTkLabel(addf, text="Artist").grid(row=1, column=0, padx=6, pady=3, sticky="w"); self.e_artist.grid(row=1, column=1, padx=6, pady=3, sticky="ew")
        ctk.CTkLabel(addf, text="Title").grid(row=2, column=0, padx=6, pady=3, sticky="w"); self.e_title.grid(row=2, column=1, padx=6, pady=3, sticky="ew")
        ctk.CTkLabel(addf, text="Album").grid(row=3, column=0, padx=6, pady=3, sticky="w"); self.e_album.grid(row=3, column=1, padx=6, pady=3, sticky="ew")
        ctk.CTkLabel(addf, text="Seconds").grid(row=4, column=0, padx=6, pady=3, sticky="w"); self.e_secs.grid(row=4, column=1, padx=6, pady=3, sticky="w")
        abtn = ctk.CTkFrame(addf, fg_color="transparent")
        abtn.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(abtn, text="Добавить в конец очереди", command=self._add_to_queue).pack(side="left", padx=4)
        ctk.CTkButton(abtn, text="Найти сейчас (приоритет)", fg_color="#5a4b8a", hover_color="#483c70", command=lambda: self._find_now(single=False)).pack(side="left", padx=4)
        ctk.CTkButton(abtn, text="Найти только этот трек", fg_color="#5a4b8a", hover_color="#483c70", command=lambda: self._find_now(single=True)).pack(side="left", padx=4)

        tools = ctk.CTkFrame(right)
        tools.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        ctk.CTkLabel(tools, text="Инструменты", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=2, padx=6, pady=(6,4), sticky="w")
        ctk.CTkButton(tools, text="Открыть лог", command=self._open_log).grid(row=1, column=0, padx=6, pady=6, sticky="ew")
        ctk.CTkButton(tools, text="Импорт CSV (с дедупликацией)", command=self._import_csv).grid(row=1, column=1, padx=6, pady=6, sticky="ew")
        tools.grid_columnconfigure((0,1), weight=1)

        info = ctk.CTkFrame(right)
        info.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        info.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(info, text="Последнее успешное скачивание (OK):").grid(row=0, column=0, padx=6, pady=4, sticky="w")
        self.last_dl_label = ctk.CTkLabel(info, text="—")
        self.last_dl_label.grid(row=0, column=1, padx=6, pady=4, sticky="w")
        ctk.CTkLabel(info, text="Свободно на диске:").grid(row=1, column=0, padx=6, pady=4, sticky="w")
        self.disk_label = ctk.CTkLabel(info, text="—")
        self.disk_label.grid(row=1, column=1, padx=6, pady=4, sticky="w")

        logf = ctk.CTkFrame(right)
        logf.grid(row=3, column=0, sticky="nsew", padx=6, pady=6)
        logf.grid_rowconfigure(1, weight=1)
        logf.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(logf, text="Журнал / статус", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=6, pady=(6,4), sticky="w")
        self.log_text = ctk.CTkTextbox(logf, wrap="word")
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)

        self.selected_uid: str | None = None

    # ---------------- helpers
    def _ensure_worker_labels(self, count: int):
        try:
            count = max(1, min(int(count or 1), 6))
        except Exception:
            count = 1
        # rebuild labels and per-worker progress bars
        for w in self.workers_frame.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        self.worker_labels = {}
        self.worker_bars = {}
        self.worker_pcts = {}
        for i in range(count):
            lbl = ctk.CTkLabel(self.workers_frame, text=f"Воркер {i+1}: ожидание", anchor="w")
            lbl.grid(row=i, column=0, sticky="w", padx=2, pady=1)
            bar = ctk.CTkProgressBar(self.workers_frame)
            bar.grid(row=i, column=1, sticky="ew", padx=6, pady=1)
            bar.set(0)
            pct = ctk.CTkLabel(self.workers_frame, text="0%", width=42)
            pct.grid(row=i, column=2, sticky="e", padx=2, pady=1)
            self.worker_labels[i] = lbl
            self.worker_bars[i] = bar
            self.worker_pcts[i] = pct

    def _set_worker_status(self, wid, text: str, progress: float | None = None):
        try:
            wid = int(wid)
        except Exception:
            return
        if wid not in self.worker_labels:
            # expand if needed
            self._ensure_worker_labels(max(len(self.worker_labels), wid + 1))
        lbl = self.worker_labels.get(wid)
        if lbl:
            try:
                lbl.configure(text=f"Воркер {wid+1}: {text}")
            except Exception:
                pass
        if progress is not None:
            try:
                p = max(0.0, min(1.0, float(progress)))
                bar = self.worker_bars.get(wid)
                pct = self.worker_pcts.get(wid)
                if bar:
                    bar.set(p)
                if pct:
                    pct.configure(text=f"{int(p*100)}%")
            except Exception:
                pass

    def _apply_settings_from_ui(self):
        self.settings.output_dir = self.out_var.get().strip() or self.settings.output_dir
        self.settings.queue_file = self.queue_var.get().strip() or self.settings.queue_file
        self.settings.target_format = self.format_var.get()
        self.settings.quality_mode = QUALITY_OPTIONS.get(self.quality_var.get(), "high")
        self.settings.stall_restart_minutes = STALL_OPTIONS.get(self.stall_var.get(), 20)
        os.makedirs(self.settings.output_dir, exist_ok=True)
        ensure_queue_file(self.settings.queue_file)
        self.manager.settings = self.settings
        save_settings(self.settings)

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")

    def _load_initial_queue(self):
        ensure_queue_file(self.settings.queue_file)
        self.manager.load_queue(self.settings.queue_file)
        self._refresh_queue_view()

    def _queue_sort_key(self, t: "TrackTask"):
        # active/processing tracks first so "сейчас ищутся/качаются" are on top
        active = {TaskStatus.SEARCHING, TaskStatus.SELECTING, TaskStatus.DOWNLOADING, TaskStatus.CONVERTING, TaskStatus.TAGGING}
        if t.status in active:
            return (0, t.status.value)
        if t.status == TaskStatus.QUEUED:
            return (1, 0)
        if t.status == TaskStatus.PENDING:
            return (2, 0)
        if t.status in (TaskStatus.FAILED, TaskStatus.MISS):
            return (3, 0)
        # ok/skipped/canceled last
        return (4, 0)

    def _queue_line(self, idx: int, t: "TrackTask") -> str:
        st = (t.status.value if t.status else "").ljust(11)
        name = (t.display_name or "").strip()
        info = " ".join(x for x in [(t.quality_label or "").strip(), (t.source or "").strip(), (t.phase or "").strip()] if x)
        note = (t.note or "").strip()
        extra = info or note
        if extra:
            extra = " | " + extra
        return f"{idx:>4}. {st}  {name}{extra}"

    def _refresh_queue_view(self):
        # lightweight text render (replaces heavy per-row widgets)
        job = getattr(self, "_queue_render_job", None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._queue_render_job = None
        with self.manager._lock:
            tasks = list(self.manager.tasks)
        try:
            tasks = sorted(tasks, key=self._queue_sort_key)
        except Exception:
            pass
        self._queue_tasks_snapshot = tasks
        self.overall_label.configure(text=f"{self.manager._done_count} / {len(tasks)}")
        total = max(1, len(tasks))
        self.overall_bar.set(self.manager._done_count / total)
        lines = []
        limit = min(len(tasks), 2000)
        for i, t in enumerate(tasks[:limit], 1):
            try:
                lines.append(self._queue_line(i, t))
            except Exception:
                lines.append(f"{i:>4}. {t.display_name}")
        if len(tasks) > limit:
            lines.append(f"... и ещё {len(tasks) - limit} треков (всего {len(tasks)})")
        text = "\n".join(lines) if lines else "Очередь пуста."
        try:
            self.queue_text.configure(state="normal")
            self.queue_text.delete("1.0", "end")
            self.queue_text.insert("1.0", text)
            self.queue_text.configure(state="disabled")
        except Exception:
            pass
        self._last_queue_reorder_ts = time.time()

    def _render_queue_chunk(self, batch: int = 20):
        # compatibility no-op: rendering is now single-pass lightweight text
        self._queue_render_job = None

    def _select(self, uid: str):
        self.selected_uid = uid

    def _tick_status(self):
        try:
            ts = self._last_success_ts or getattr(self.manager, "last_success_ts", 0) or 0
            if ts and ts > 0:
                tstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                name = getattr(self, "_last_success_name", "") or ""
                self.last_dl_label.configure(text=(f"{tstr}  —  {name}" if name else tstr))
            else:
                self.last_dl_label.configure(text="—")
            out = self.out_var.get().strip() or self.settings.output_dir
            if out and os.path.exists(os.path.splitdrive(out)[0] + os.sep or out):
                try:
                    usage = shutil.disk_usage(out if os.path.exists(out) else os.path.splitdrive(out)[0] + "\\")
                    self.disk_label.configure(text=f"{usage.free/2**30:.1f} ГБ свободно")
                except Exception:
                    pass
        except Exception:
            pass
        self.after(2000, self._tick_status)

    # ---------------- manager events
    def _on_manager_event(self, ev: ProgressEvent):
        self.ui_queue.put(ev)

    def _poll_ui_queue(self):
        try:
            while True:
                ev = self.ui_queue.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.after(150, self._poll_ui_queue)

    def _handle_event(self, ev: ProgressEvent):
        if ev.kind == "log":
            self._append_log(ev.message)
        elif ev.kind == "refresh_queue":
            self._refresh_queue_view()
        elif ev.kind == "stall":
            self._append_log(f"[STALL] {ev.message}")
        elif ev.kind == "overall":
            d = ev.overall_done or 0; t = ev.overall_total or 0
            self.overall_label.configure(text=f"{d} / {t}")
            self.overall_bar.set((d / t) if t else 0)
            if ev.message:
                self._append_log(ev.message)
        elif ev.kind == "scan_progress":
            data = ev.data or {}
            cur = data.get("current", 0); tot = data.get("total", 0); sk = data.get("skipped", 0)
            prog = (float(cur)/float(tot)) if tot else None
            self._set_worker_status(0, f"сканирование: {cur}/{tot} (уже есть: {sk})", progress=prog)
            self._append_log(ev.message or f"сканирование {cur}/{tot}")
        elif ev.kind == "scan_done":
            self._set_worker_status(0, "сканирование завершено", progress=1.0)
            self._append_log(f"сканирование завершено, пропущено: {ev.message}")
        elif ev.kind == "workers":
            data = ev.data or {}
            count = int(data.get("count") or 0)
            if count > 0:
                self._ensure_worker_labels(count)
                self._append_log(f"активных воркеров: {count}")
            else:
                # mark all idle
                for wid in list(self.worker_labels.keys()):
                    self._set_worker_status(wid, "ожидание", progress=0.0)
        elif ev.kind == "worker_idle":
            data = ev.data or {}
            wid = data.get("wid")
            if wid is not None:
                self._set_worker_status(wid, "ожидание", progress=0.0)
        elif ev.kind == "track":
            uid = ev.task_uid
            data = ev.data or {}
            phase = data.get("phase") or ev.message or ""
            prog = data.get("progress")
            status = ev.status or data.get("status") or ""
            wid = data.get("wid")
            # find name
            name = uid
            with self.manager._lock:
                for t in self.manager.tasks:
                    if t.uid == uid:
                        name = t.display_name
                        break
            if wid is not None:
                short = phase or status or ""
                self._set_worker_status(wid, f"{name} — {short}"[:120], progress=prog)
            # terminal states: keep bar full, and mark idle-like text only when appropriate
            if wid is not None and status in (TaskStatus.OK.value, TaskStatus.SKIPPED.value, TaskStatus.MISS.value, TaskStatus.FAILED.value, TaskStatus.CANCELED.value):
                # leave final phase visible briefly; worker loop will emit next task or idle
                if prog is None:
                    self._set_worker_status(wid, f"{name} — {short}"[:120], progress=1.0)
            if status == TaskStatus.OK.value:
                self._last_success_ts = time.time()
                out_file = data.get("out_file") or ""
                if not out_file and uid:
                    with self.manager._lock:
                        for tt in self.manager.tasks:
                            if tt.uid == uid:
                                out_file = tt.out_file or ""
                                break
                self._last_success_name = os.path.basename(out_file) if out_file else ""
            # update done count periodically
            with self.manager._lock:
                done = sum(1 for t in self.manager.tasks if t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED))
                total = len(self.manager.tasks)
            self.overall_label.configure(text=f"{done} / {total}")
            self.overall_bar.set((done/total) if total else 0)
            # if a track became active or finished, refresh lightweight queue view (throttled)
            active_statuses = {TaskStatus.SEARCHING.value, TaskStatus.SELECTING.value, TaskStatus.DOWNLOADING.value, TaskStatus.CONVERTING.value, TaskStatus.TAGGING.value}
            if status == TaskStatus.OK.value or status in active_statuses or phase in ("старт",):
                now = time.time()
                # cheap text refresh throttled to avoid lag
                if now - getattr(self, "_last_queue_reorder_ts", 0) > 2.0:
                    self._last_queue_reorder_ts = now
                    self._refresh_queue_view()

    # ---------------- actions
    def _choose_out(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or os.path.expanduser("~"))
        if d:
            self.out_var.set(d)
            if not self.queue_var.get().strip():
                self.queue_var.set(os.path.join(d, "queue.csv"))

    def _choose_queue_file(self):
        initial = self.queue_var.get().strip() or os.path.join(self.out_var.get().strip() or self.settings.output_dir, "queue.csv")
        path = filedialog.asksaveasfilename(
            title="Выбрать или создать файл очереди (CSV)",
            initialdir=os.path.dirname(initial) if os.path.dirname(initial) else os.path.expanduser("~"),
            initialfile=os.path.basename(initial) or "queue.csv",
            defaultextension=".csv",
            filetypes=[("CSV queue", "*.csv"), ("All", "*.*")],
        )
        if not path:
            return
        self.queue_var.set(path)
        self._apply_settings_from_ui()
        ensure_queue_file(self.settings.queue_file)
        self.manager.load_queue(self.settings.queue_file)
        self._refresh_queue_view()
        self._append_log(f"активный файл очереди: {self.settings.queue_file}")

    def _open_out_folder(self):
        self._apply_settings_from_ui()
        d = self.settings.output_dir
        os.makedirs(d, exist_ok=True)
        open_path(d)

    def _sources_dialog(self):
        win = ctk.CTkToplevel(self)
        win.title("Источники поиска")
        win.geometry("420x300")
        vars = {}
        cur = set(self.settings.sources or ALL_SOURCES)
        for i, s in enumerate(ALL_SOURCES):
            v = ctk.BooleanVar(value=s in cur)
            vars[s] = v
            ctk.CTkCheckBox(win, text=s, variable=v).pack(anchor="w", padx=20, pady=8)
        def save():
            sel = [s for s in ALL_SOURCES if vars[s].get()]
            if not sel:
                messagebox.showwarning("Источники", "Нужен хотя бы один источник")
                return
            self.settings.sources = sel
            save_settings(self.settings)
            self.manager.settings = self.settings
            win.destroy()
        ctk.CTkButton(win, text="Сохранить", command=save).pack(pady=16)

    def _settings_dialog(self):
        win = ctk.CTkToplevel(self)
        win.title("Настройки")
        win.geometry("520x420")
        frm = ctk.CTkFrame(win)
        frm.pack(fill="both", expand=True, padx=12, pady=12)
        frm.grid_columnconfigure(1, weight=1)
        w_var = ctk.IntVar(value=int(self.settings.workers or 3))
        t_var = ctk.IntVar(value=int(self.settings.search_timeout_s or 80))
        try_var = ctk.IntVar(value=int(self.settings.try_candidates or 3))
        c_var = ctk.IntVar(value=int(self.settings.custom_bitrate_kbps or 320))
        skip_var = ctk.BooleanVar(value=bool(self.settings.skip_existing))
        m3u_var = ctk.BooleanVar(value=bool(self.settings.write_m3u))
        rep_var = ctk.BooleanVar(value=bool(self.settings.write_report))

        def row(r, label, widget):
            ctk.CTkLabel(frm, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=8)
            widget.grid(row=r, column=1, sticky="ew", padx=8, pady=8)
        row(0, "Параллельных воркеров (1-6)", ctk.CTkEntry(frm, textvariable=w_var))
        row(1, "Таймаут поиска, сек", ctk.CTkEntry(frm, textvariable=t_var))
        row(2, "Кандидатов пробовать на трек", ctk.CTkEntry(frm, textvariable=try_var))
        row(3, "Свой битрейт (kbps) для custom", ctk.CTkEntry(frm, textvariable=c_var))
        ctk.CTkCheckBox(frm, text="Пропускать уже существующие файлы", variable=skip_var).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=8)
        ctk.CTkCheckBox(frm, text="Писать M3U плейлист", variable=m3u_var).grid(row=5, column=0, columnspan=2, sticky="w", padx=8, pady=8)
        ctk.CTkCheckBox(frm, text="Писать CSV отчёт", variable=rep_var).grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=8)
        def save():
            self.settings.workers = max(1, min(6, int(w_var.get() or 3)))
            self.settings.search_timeout_s = max(20, int(t_var.get() or 80))
            self.settings.try_candidates = max(1, min(8, int(try_var.get() or 3)))
            self.settings.custom_bitrate_kbps = max(64, min(320, int(c_var.get() or 320)))
            self.settings.skip_existing = bool(skip_var.get())
            self.settings.write_m3u = bool(m3u_var.get())
            self.settings.write_report = bool(rep_var.get())
            save_settings(self.settings)
            self.manager.settings = self.settings
            win.destroy()
        ctk.CTkButton(frm, text="Сохранить", command=save).grid(row=7, column=0, columnspan=2, pady=16)

    def _show_readme(self):
        win = ctk.CTkToplevel(self)
        win.title("Формат списка (README)")
        win.geometry("640x420")
        tb = ctk.CTkTextbox(win, wrap="word")
        tb.pack(fill="both", expand=True, padx=10, pady=10)
        tb.insert("1.0", README_TEXT)
        tb.configure(state="disabled")

    def _import_csv(self):
        path = filedialog.askopenfilename(title="Выбрать CSV для импорта в текущую очередь", filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not path:
            return
        self._apply_settings_from_ui()
        try:
            tasks = import_csv(path, dedupe=True)
            added = append_tasks(self.settings.queue_file, tasks)
            self.manager.load_queue(self.settings.queue_file)
            self._refresh_queue_view()
            self._append_log(f"импорт в текущую очередь: {len(tasks)} строк из файла, добавлено уникальных: {added}")
        except Exception as e:
            messagebox.showerror("Импорт", str(e))

    def _add_to_queue(self):
        self._apply_settings_from_ui()
        artist = self.e_artist.get().strip()
        title = self.e_title.get().strip()
        album = self.e_album.get().strip()
        try:
            secs = int(float(self.e_secs.get().strip() or 0))
        except Exception:
            secs = 0
        if not artist and not title:
            messagebox.showwarning("Добавление", "Укажите хотя бы Artist или Title")
            return
        t = TrackTask(artist=artist, title=title, album=album, seconds=secs)
        self.manager.add_task(t, to_front=False)
        self.manager.load_queue(self.settings.queue_file)
        self._refresh_queue_view()
        self._append_log(f"добавлен в очередь: {t.display_name}")

    def _find_now(self, single: bool):
        self._apply_settings_from_ui()
        artist = self.e_artist.get().strip()
        title = self.e_title.get().strip()
        album = self.e_album.get().strip()
        try:
            secs = int(float(self.e_secs.get().strip() or 0))
        except Exception:
            secs = 0
        if not artist and not title:
            messagebox.showwarning("Поиск", "Укажите хотя бы Artist или Title")
            return
        t = TrackTask(artist=artist, title=title, album=album, seconds=secs)
        # add to front if not exists, and run priority
        with self.manager._lock:
            existing = None
            for x in self.manager.tasks:
                if norm_text(x.artist) == norm_text(artist) and norm_text(x.title) == norm_text(title):
                    existing = x; break
            if not existing:
                self.manager.tasks.insert(0, t)
                self.manager._total_count = len(self.manager.tasks)
                uid = t.uid
            else:
                existing.status = TaskStatus.PENDING
                uid = existing.uid
        self._refresh_queue_view()
        if self.manager.is_running:
            self.manager.request_priority(uid, single_only=single)
        else:
            self.manager.start(single_uid=uid if single else None, priority_uid=None if single else uid)
        self._append_log(("поиск только этого трека: " if single else "приоритетный поиск: ") + f"{artist} - {title}")

    def _start(self):
        self._apply_settings_from_ui()
        # load from file only if queue not loaded yet; avoid resetting statuses/progress
        if not self.manager.tasks:
            self.manager.load_queue(self.settings.queue_file)
            self._refresh_queue_view()
        if not self.manager.tasks:
            messagebox.showinfo("Старт", "Очередь пуста. Импортируйте CSV или добавьте треки.")
            return
        # if everything already done, inform user
        if all(t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED) for t in self.manager.tasks):
            messagebox.showinfo("Старт", "Все задачи уже обработаны. Используйте «Повторить failed/miss» для повторной попытки нужных треков.")
            return
        if self.manager.is_running or getattr(self, "_starting", False):
            return
        self._starting = True
        n = max(1, min(int(self.settings.workers or 1), 6))
        self._ensure_worker_labels(n)
        self._append_log("сканирование уже скачанных файлов перед стартом...")

        def prescan_progress(cur, tot, sk):
            self.ui_queue.put(ProgressEvent(kind="scan_progress", message=f"сканирование: {cur}/{tot} (уже есть: {sk})", data={"current": cur, "total": tot, "skipped": sk}))

        def run():
            try:
                self.manager.prescan_existing(progress_cb=prescan_progress)
                self.ui_queue.put(ProgressEvent(kind="log", message="сканирование завершено, запуск загрузки..."))
                self.ui_queue.put(ProgressEvent(kind="scan_done", message="done"))
                # refresh queue view after scan
                self.ui_queue.put(ProgressEvent(kind="refresh_queue"))
            except Exception as e:
                self.ui_queue.put(ProgressEvent(kind="log", message=f"ошибка сканирования: {e}"))
            finally:
                try:
                    self.manager.start()
                except Exception as e:
                    self.ui_queue.put(ProgressEvent(kind="log", message=f"ошибка запуска: {e}"))
                self._starting = False

        threading.Thread(target=run, daemon=True).start()

    def _pause(self):
        self.manager.pause()

    def _resume(self):
        self.manager.resume()

    def _stop(self):
        self.manager.stop()

    def _retry_failed(self):
        with self.manager._lock:
            n = 0
            for t in self.manager.tasks:
                if t.status in (TaskStatus.FAILED, TaskStatus.MISS):
                    t.status = TaskStatus.PENDING
                    t.progress = 0.0
                    t.phase = ""
                    n += 1
            # adjust done count so overall progress reflects pending retries
            self.manager._done_count = sum(1 for t in self.manager.tasks if t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED))
        self._refresh_queue_view()
        self._append_log(f"сброшено для повтора: {n}")

    def _open_log(self):
        # main app log + jobs results
        candidates = [
            os.path.join(self.settings.output_dir, "_jobs", "results.jsonl"),
            os.path.join(self.settings.output_dir, "app.log"),
        ]
        for p in candidates:
            if os.path.exists(p):
                open_path(p)
                return
        # create placeholder
        p = os.path.join(self.settings.output_dir, "app.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write("")
        open_path(p)

    # (playback/open-track functions removed by request)

    def _export_report(self):
        self._apply_settings_from_ui()
        out = self.settings.output_dir
        os.makedirs(out, exist_ok=True)
        rep = os.path.join(out, "report.csv")
        m3u = os.path.join(out, "playlist.m3u8")
        with self.manager._lock:
            tasks = list(self.manager.tasks)
        with open(rep, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Artist","Title","Album","Seconds","Status","Source","Quality","Match","DurDelta_s","OutFile","Note"])
            for t in tasks:
                w.writerow([t.artist,t.title,t.album,t.seconds,t.status.value,t.source,t.quality_label,t.match,t.dur_delta_s,t.out_file,t.note])
        if self.settings.write_m3u:
            lines = ["#EXTM3U"]
            for t in tasks:
                if t.status == TaskStatus.OK and t.out_file and os.path.exists(t.out_file):
                    lines.append(f"#EXTINF:{t.seconds},{os.path.splitext(os.path.basename(t.out_file))[0]}")
                    lines.append(os.path.basename(t.out_file))
            with open(m3u, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        self._append_log(f"экспорт: {rep}" + (f", {m3u}" if self.settings.write_m3u else ""))
        messagebox.showinfo("Экспорт", f"Сохранено:\n{rep}" + (f"\n{m3u}" if self.settings.write_m3u else ""))

    def _on_close(self):
        try:
            self._apply_settings_from_ui()
            self.manager.save_queue(self.settings.queue_file)
            self.manager.stop()
        except Exception:
            pass
        self.destroy()


def open_path(path: str):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        try:
            webbrowser.open(path)
        except Exception:
            pass
