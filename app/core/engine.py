from __future__ import annotations

import copy
import csv
import fractions
import gc
import json
import os
import re
import shutil
import threading
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Optional, Tuple

import av
import mutagen
from mutagen.easyid3 import EasyID3
from musicdl import musicdl as m

from .models import TrackTask, TaskStatus, AppSettings

LOSSLESS = {"flac", "wav", "ape", "alac", "wv", "tta", "dsf", "dff"}
LOSSY = {"mp3", "m4a", "mp4", "aac", "ogg", "opus"}

ProgressCB = Callable[[str, dict], None]  # (event_kind, payload)


# ---------------------------- text/matching (adapted from yadl_core)
def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\[\(]\s*(?:official\s+)?(?:music\s+)?(?:video|audio|remaster(?:ed)?|bonus|mono|stereo)[^)\]]*[\)\]]", " ", s)
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    r = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        r = max(r, 0.93)
    return r


def artist_score(target_artists: str, cand_artists: str) -> float:
    ta, ca = norm(target_artists), norm(cand_artists)
    if not ta or not ca:
        return 0.0
    best = 0.0
    for part in re.split(r"[,;&]|(?:feat\.?)|(?:при уч\.)| и ", ca):
        part = part.strip()
        if not part:
            continue
        for tp in re.split(r"[,;&]|(?:feat\.?)", ta):
            tp = tp.strip()
            if not tp:
                continue
            best = max(best, sim(tp, part))
    return best


def title_score(target_title: str, cand_title: str) -> float:
    tt, ct = norm(target_title), norm(re.sub(r"\[[^\]]*\]", "", cand_title or ""))
    return max(sim(tt, ct), SequenceMatcher(None, tt, ct.replace("  ", " ")).ratio())


def cand_kbps(c) -> Optional[float]:
    try:
        if getattr(c, "bitrate", None):
            return float(c.bitrate)
    except Exception:
        pass
    try:
        fsb = getattr(c, "file_size_bytes", None)
        dur = getattr(c, "duration_s", None)
        if fsb and fsb > 0 and dur and dur > 0:
            return fsb * 8 / dur / 1000
    except Exception:
        pass
    return None


def rank_candidates(cands, artist: str, title: str, secs: int, target_format: str, quality_mode: str):
    out = []
    ntitle = norm(title)
    nartist = norm(artist)
    nfull = f"{nartist} {ntitle}".strip()
    for c in cands:
        try:
            if not getattr(c, "with_valid_download_url", False) or not getattr(c, "ext", None):
                continue
            csong = getattr(c, "song_name", "") or ""
            cfull = f"{norm(getattr(c,'singers','') or '')} {norm(csong)}"[0:400]
            tsim = title_score(title, csong)
            asim = artist_score(artist, getattr(c, "singers", "") or "")
            full = sim(nfull, cfull) if nfull else tsim
            text = max(0.57 * tsim + 0.43 * asim, 0.97 * full) if nartist else max(tsim, 0.97 * sim(ntitle, norm(csong)))
            durc = getattr(c, "duration_s", None)
            if durc and secs:
                dd = abs(float(durc) - float(secs))
                dscore = max(0.0, 1 - dd / 45)
            else:
                dd, dscore = None, 0.4
            match = 0.78 * text + 0.22 * dscore
            # filters
            if tsim < 0.45 or (nartist and asim < 0.35 and full < 0.6):
                continue
            kbps = cand_kbps(c)
            if kbps is not None and not (8 <= kbps <= 5000):
                continue
            fsb = getattr(c, "file_size_bytes", None)
            if fsb and fsb < 80_000:
                continue
            if durc and secs and secs > 40 and float(durc) < 20:
                continue
            tier, qlabel = _quality_tier(c, target_format, quality_mode, kbps)
            out.append({
                "cand": c, "match": round(match, 3), "tsim": round(tsim, 3),
                "asim": round(asim, 3), "dur_delta_s": None if dd is None else round(dd),
                "tier": tier, "qlabel": qlabel, "kbps": None if kbps is None else round(kbps),
                "source": getattr(c, "source", ""),
                "remote_title": csong, "remote_artists": getattr(c, "singers", "") or "",
            })
        except Exception:
            continue
    out.sort(key=lambda x: (x["tier"], -x["match"], -(x["kbps"] or 0)))
    return out


def _quality_tier(c, target_format: str, quality_mode: str, kbps: Optional[float]):
    ext = (getattr(c, "ext", "") or "").lower().lstrip(".")
    tf = (target_format or "mp3").lower()
    # prefer direct match of good quality
    if tf == "mp3":
        if ext == "mp3" and kbps and 250 <= kbps <= 400:
            return 0, "mp3 ~320"
        if ext in LOSSLESS:
            return 1, f"{ext}→mp3"
        if ext == "mp3" and kbps and 160 <= kbps < 250:
            return 2, f"mp3 {kbps:.0f}k"
        if ext in {"m4a", "mp4", "aac"} and kbps and kbps >= 128:
            return 3, f"{ext} {kbps:.0f}k"
        if ext == "mp3" and kbps:
            return 4, f"mp3 {kbps:.0f}k"
    else:
        if ext == tf:
            return 0, f"{ext} (прямой)"
        if ext in LOSSLESS:
            return 1, f"{ext}→{tf}"
        if ext in LOSSY and kbps and kbps >= 192:
            return 2, f"{ext} {kbps:.0f}k→{tf}"
    if ext in LOSSLESS:
        return 1, f"{ext}→{tf}"
    if ext in LOSSY:
        return 4, f"{ext}→{tf}"
    return 9, "other"


# ---------------------------- file/audio utils
def sanitize_name(s: str, maxlen: int = 150) -> str:
    s = (s or "").strip().replace('"', "").strip()
    s = re.sub(r'[<>:/\\|?*\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:maxlen].rstrip(" .")


def render_filename(template: str, artist: str, title: str, album: str) -> str:
    name = template or "{artist} - {title}"
    name = name.replace("{artist}", sanitize_name(artist)).replace("{title}", sanitize_name(title)).replace("{album}", sanitize_name(album))
    name = sanitize_name(name, 200) or "track"
    return name


def target_path(out_dir: str, template: str, artist: str, title: str, album: str, ext: str) -> str:
    return os.path.join(out_dir, render_filename(template, artist, title, album) + "." + ext.lstrip("."))


def _resampled(rs, frame):
    try:
        ret = rs.resample(frame)
    except Exception:
        return []
    if isinstance(ret, tuple):
        first = ret[0]
        return list(first) if isinstance(first, (list, tuple)) else [first]
    if isinstance(ret, (list, tuple)):
        return list(ret)
    return [ret]


def transcode(src: str, dst: str, target_format: str, bitrate_kbps: int = 320) -> int:
    tf = target_format.lower().lstrip(".")
    fmt_map = {"mp3": "mp3", "m4a": "ipod", "aac": "adts", "flac": "flac", "opus": "opus", "ogg": "ogg", "wav": "wav"}
    codec_map = {"mp3": "mp3", "m4a": "aac", "aac": "aac", "flac": "flac", "opus": "libopus", "ogg": "libvorbis", "wav": "pcm_s16le"}
    out_fmt = fmt_map.get(tf, tf)
    codec = codec_map.get(tf, "mp3")
    inp = out = None
    frames = 0
    try:
        inp = av.open(src)
        out = av.open(dst, "w", format=out_fmt)
        st = out.add_stream(codec, rate=44100)
        st.layout = "stereo"
        if tf in {"mp3", "m4a", "aac", "opus", "ogg"}:
            st.bit_rate = int(bitrate_kbps) * 1000
        # flac/wav - lossless/pcm, bitrate ignored
        rs = av.AudioResampler(format="s16" if tf in {"mp3","wav","flac"} else "fltp", layout="stereo", rate=44100)
        for frame in inp.decode(audio=0):
            for af in _resampled(rs, frame):
                if af is None:
                    continue
                af.pts = None
                for p in st.encode(af):
                    out.mux(p)
                frames += 1
        for p in st.encode(None):
            out.mux(p)
    except Exception:
        try:
            if out: out.close()
        except Exception:
            pass
        try:
            if inp: inp.close()
        except Exception:
            pass
        out = inp = None
        try:
            if os.path.exists(dst): os.remove(dst)
        except Exception:
            pass
        raise
    finally:
        try:
            if out: out.close()
        except Exception:
            pass
        try:
            if inp: inp.close()
        except Exception:
            pass
    if frames == 0:
        try:
            if os.path.exists(dst): os.remove(dst)
        except Exception:
            pass
        raise RuntimeError("транскод не дал кадров")
    return frames


def tag_file(path: str, artist: str, title: str, album: str = ""):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    try:
        if ext == "mp3":
            try:
                audio = EasyID3(path)
            except Exception:
                try:
                    mutagen.File(path).add_tags()
                except Exception:
                    pass
                audio = EasyID3()
            audio["artist"] = artist
            audio["title"] = title
            if album:
                audio["album"] = album
            audio.save(path)
        else:
            mfile = mutagen.File(path, easy=True)
            if mfile is None:
                return
            if mfile.tags is None:
                mfile.add_tags()
            mfile["artist"] = [artist]
            mfile["title"] = [title]
            if album:
                mfile["album"] = [album]
            mfile.save()
    except Exception:
        pass


def remove_retry(path: str, tries: int = 6):
    for i in range(tries):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except PermissionError:
            gc.collect()
            time.sleep(0.4 * (i + 1))


def move_retry(src: str, dst: str, tries: int = 6):
    for i in range(tries):
        try:
            shutil.move(src, dst)
            return
        except PermissionError:
            gc.collect()
            time.sleep(0.4 * (i + 1))
    shutil.move(src, dst)


def probe_length(path: str):
    try:
        mul = mutagen.File(path)
        if mul and mul.info and getattr(mul.info, "length", 0):
            return round(mul.info.length), (getattr(mul.info, "bitrate", 0) // 1000)
    except Exception:
        pass
    return None, None


def effective_bitrate_kbps(settings: AppSettings) -> int:
    mode = settings.quality_mode
    if mode == "best":
        return 320
    if mode == "high":
        return 320
    if mode == "medium":
        return 192
    if mode == "low":
        return 128
    if mode == "custom":
        return max(64, min(320, int(settings.custom_bitrate_kbps or 320)))
    return 320


class DownloadEngine:
    def __init__(self, settings: AppSettings, progress_cb: Optional[ProgressCB] = None):
        self.settings = settings
        self.progress_cb = progress_cb or (lambda k, p: None)
        self._client = None
        self._client_lock = threading.Lock()
        self.last_success_ts: float = 0.0
        self.cancel_event = threading.Event()

    def emit(self, kind: str, **payload):
        try:
            self.progress_cb(kind, payload)
        except Exception:
            pass

    def ensure_client(self):
        with self._client_lock:
            if self._client is not None:
                return self._client
            sources = [s for s in self.settings.sources if s]
            if not sources:
                sources = ["NeteaseMusicClient", "KuwoMusicClient", "BilibiliMusicClient", "YouTubeMusicClient"]
            out_dir = self.settings.output_dir
            staging = os.path.join(out_dir, "_staging")
            os.makedirs(staging, exist_ok=True)
            self._client = m.MusicClient(
                music_sources=sources,
                init_music_clients_cfg={
                    s: {"work_dir": os.path.join(staging, "search"), "disable_print": True,
                        "max_retries": 1, "search_size_per_source": 8}
                    for s in sources
                },
                clients_threadings={s: 4 for s in sources},
            )
            return self._client

    def search_all(self, keyword: str, budget: float) -> dict:
        client = self.ensure_client()
        res, done = {}, {}
        sources = list(client.music_clients.keys())

        def run(src):
            try:
                res[src] = client.music_clients[src].search(keyword=keyword) or []
            except Exception:
                res[src] = []
            done[src] = True

        threads = [threading.Thread(target=run, args=(s,), daemon=True) for s in sources]
        for t in threads:
            t.start()
        deadline = time.time() + budget
        for t in threads:
            t.join(max(0.1, deadline - time.time()))
        return {s: res.get(s) or [] for s in sources}

    def process_task(self, task: TrackTask, worker_id: Optional[int] = None) -> TrackTask:
        if self.cancel_event.is_set():
            task.status = TaskStatus.CANCELED
            task.phase = "отменено"
            return task
        t0 = time.time()
        settings = self.settings
        tf = (settings.target_format or "mp3").lower()
        br = effective_bitrate_kbps(settings)
        os.makedirs(settings.output_dir, exist_ok=True)
        staging = os.path.join(settings.output_dir, "_staging")
        os.makedirs(staging, exist_ok=True)
        fp = target_path(settings.output_dir, settings.filename_template, task.artist, task.title, task.album, tf)

        # skip existing
        if settings.skip_existing and os.path.exists(fp) and os.path.getsize(fp) > 50_000:
            length, _ = probe_length(fp)
            task.status = TaskStatus.SKIPPED
            task.out_file = fp
            task.note = "уже есть (пропуск)"
            task.phase = "готово"
            task.dur_delta_s = None if not (length and task.seconds) else length - task.seconds
            task.progress = 1.0
            self.emit("track", uid=task.uid, wid=worker_id, status=task.status.value, phase=task.phase, progress=1.0, note=task.note, out_file=fp)
            return task

        keyword = f"{task.artist} {task.title}".strip()
        task.status = TaskStatus.SEARCHING
        task.phase = f"поиск: {keyword[:60]}"
        task.progress = 0.05
        self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=task.progress)

        try:
            bysrc = self.search_all(keyword, budget=float(settings.search_timeout_s))
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.note = f"ошибка поиска: {e}"
            task.phase = "ошибка"
            self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=0.0, note=task.note)
            return task

        cands = [c for lst in bysrc.values() for c in lst]
        task.status = TaskStatus.SELECTING
        task.phase = f"найдено кандидатов: {len(cands)}, выбор лучшего..."
        task.progress = 0.15
        self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=task.progress, candidates=len(cands))

        ranked = rank_candidates(cands, task.artist, task.title, task.seconds, tf, settings.quality_mode)
        if not ranked:
            task.status = TaskStatus.MISS
            task.note = "не найдено подходящих версий"
            task.phase = "не найдено"
            self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=1.0, note=task.note)
            return task

        # emit candidate summary (transparent)
        best = ranked[0]
        self.emit("candidates", uid=task.uid, best=best.get("qlabel"), best_source=best.get("source"), best_match=best.get("match"), count=len(ranked))

        client = self.ensure_client()
        tries = 0
        for item in ranked:
            if self.cancel_event.is_set():
                task.status = TaskStatus.CANCELED
                task.phase = "отменено"
                return task
            if tries >= int(settings.try_candidates):
                break
            c = item["cand"]
            tries += 1
            src_name = getattr(c, "source", "?")
            ext = (getattr(c, "ext", "mp3") or "mp3").lower().lstrip(".")
            task.status = TaskStatus.DOWNLOADING
            task.phase = f"скачивание: {src_name} ({item.get('qlabel')}, match={item.get('match')})"
            task.progress = 0.25
            self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=task.progress, source=src_name, qlabel=item.get("qlabel"))
            staged = os.path.join(staging, f"{task.uid}.{ext}")
            try:
                try:
                    if os.path.exists(staged):
                        os.remove(staged)
                except Exception:
                    pass
                cc = copy.deepcopy(c)
                cc._save_path = os.path.abspath(staged)
                dl = client.music_clients[cc.source].download(song_infos=[cc], auto_supplement_song=False, num_threadings=1)
                if not dl or not os.path.exists(staged) or os.path.getsize(staged) < 80_000:
                    task.note = f"источник {src_name} не отдал валидный файл"
                    continue

                # direct keep if same format and acceptable
                direct_ok = (ext == tf)
                if tf == "mp3" and ext == "mp3":
                    kb = cand_kbps(c)
                    if kb and kb < 200 and settings.quality_mode in ("best", "high"):
                        direct_ok = False  # try to get better? but already downloaded; keep as is is fine
                    direct_ok = True
                if direct_ok and ext == tf:
                    task.status = TaskStatus.TAGGING
                    task.phase = "сохранение и теги..."
                    task.progress = 0.9
                    self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=task.progress)
                    move_retry(staged, fp)
                else:
                    task.status = TaskStatus.CONVERTING
                    task.phase = f"конвертация → {tf} {br}kbps..."
                    task.progress = 0.7
                    self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=task.progress)
                    tmp = fp + ".tmp." + tf
                    transcode(staged, tmp, tf, br)
                    gc.collect()
                    remove_retry(staged)
                    move_retry(tmp, fp)

                tag_file(fp, task.artist, task.title, task.album)
                length, _ = probe_length(fp)
                task.status = TaskStatus.OK
                task.out_file = fp
                task.source = src_name
                task.quality_label = item.get("qlabel") or ""
                task.match = item.get("match")
                task.dur_delta_s = None if not (length and task.seconds) else length - task.seconds
                task.note = f"OK: {item.get('remote_artists','')} - {item.get('remote_title','')}"[:120]
                task.phase = "готово"
                task.progress = 1.0
                self.last_success_ts = time.time()
                self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=1.0,
                          out_file=fp, source=src_name, qlabel=task.quality_label, match=task.match, note=task.note)
                return task
            except Exception as e:
                task.note = f"ошибка: {e}"
                task.phase = f"ошибка: {str(e)[:60]}"
                self.emit("track", uid=task.uid, status=TaskStatus.DOWNLOADING.value, phase=task.phase, progress=task.progress, note=task.note)
                try:
                    if os.path.exists(staged):
                        remove_retry(staged)
                except Exception:
                    pass
                continue

        if task.status != TaskStatus.OK:
            task.status = TaskStatus.FAILED if task.note else TaskStatus.MISS
            task.phase = "не удалось" if task.status == TaskStatus.FAILED else "не найдено"
            task.progress = 1.0
            self.emit("track", uid=task.uid, status=task.status.value, phase=task.phase, progress=1.0, note=task.note)
        # cleanup search junk occasionally handled by caller
        return task
