from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    SEARCHING = "searching"
    SELECTING = "selecting"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    TAGGING = "tagging"
    OK = "ok"
    SKIPPED = "skipped"
    MISS = "miss"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class TrackTask:
    artist: str = ""
    title: str = ""
    album: str = ""
    seconds: int = 0
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.PENDING
    source: str = ""
    quality_label: str = ""
    match: Optional[float] = None
    dur_delta_s: Optional[int] = None
    out_file: str = ""
    note: str = ""
    progress: float = 0.0  # 0..1 for current track phase
    speed: str = ""
    eta: str = ""
    phase: str = ""
    last_update: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{norm_text(self.artist)}||{norm_text(self.title)}"

    @property
    def display_name(self) -> str:
        a = self.artist or "?"
        t = self.title or "?"
        return f"{a} - {t}"

    def to_row(self) -> dict:
        return {
            "Artist": self.artist,
            "Title": self.title,
            "Album": self.album,
            "Seconds": str(self.seconds or ""),
        }


def norm_text(s: str) -> str:
    import re, unicodedata
    s = (s or "").lower().replace("ё", "е")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\[\(]\s*(?:official\s+)?(?:music\s+)?(?:video|audio|remaster(?:ed)?|bonus|mono|stereo)[^)\]]*[\)\]]", " ", s)
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass
class AppSettings:
    output_dir: str = ""
    queue_file: str = ""
    target_format: str = "mp3"  # mp3, m4a, flac, opus, wav
    quality_mode: str = "high"  # best, high(320), medium(192), low(128), custom
    custom_bitrate_kbps: int = 320
    prefer_lossless_source: bool = True
    workers: int = 3
    search_timeout_s: int = 80
    try_candidates: int = 3
    sources: list[str] = field(default_factory=lambda: [
        "NeteaseMusicClient", "KuwoMusicClient", "BilibiliMusicClient", "YouTubeMusicClient"
    ])
    stall_restart_minutes: int = 20  # 0 = never
    filename_template: str = "{artist} - {title}"
    skip_existing: bool = True
    dedupe_on_import: bool = True
    write_m3u: bool = True
    write_report: bool = True
    embed_cover: bool = True
    auto_retry_failed: bool = False
    auto_retry_missed_on_end: bool = False
    max_retries_per_track: int = 1
    theme: str = "dark"
    language: str = "ru"

    @classmethod
    def defaults(cls, base_dir: str = "") -> "AppSettings":
        import os
        out = base_dir or os.path.join(os.path.expanduser("~"), "Music", "MusicDL")
        q = os.path.join(out, "queue.csv")
        return cls(output_dir=out, queue_file=q)


@dataclass
class ProgressEvent:
    kind: str  # overall, track, log, result, stall
    task_uid: Optional[str] = None
    message: str = ""
    track_progress: Optional[float] = None
    overall_done: Optional[int] = None
    overall_total: Optional[int] = None
    status: Optional[str] = None
    data: dict = field(default_factory=dict)
