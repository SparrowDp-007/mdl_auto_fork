from __future__ import annotations

import csv
import os
from typing import Iterable, List, Tuple

from .models import TrackTask, norm_text

COLUMNS = ["Artist", "Title", "Album", "Seconds"]


def ensure_queue_file(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()


def read_queue(path: str) -> List[TrackTask]:
    if not os.path.exists(path):
        return []
    tasks: List[TrackTask] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            secs = 0
            try:
                secs = int(float(row.get("Seconds") or 0))
            except Exception:
                secs = 0
            tasks.append(TrackTask(
                artist=(row.get("Artist") or "").strip(),
                title=(row.get("Title") or "").strip(),
                album=(row.get("Album") or "").strip(),
                seconds=secs,
            ))
    return tasks


def write_queue(path: str, tasks: Iterable[TrackTask]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for t in tasks:
            w.writerow(t.to_row())
    os.replace(tmp, path)


def append_tasks(path: str, tasks: Iterable[TrackTask]) -> int:
    ensure_queue_file(path)
    existing = read_queue(path)
    existing_keys = {(norm_text(t.artist), norm_text(t.title)) for t in existing}
    added = 0
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        for t in tasks:
            k = (norm_text(t.artist), norm_text(t.title))
            if not k[0] and not k[1]:
                continue
            if k in existing_keys:
                continue
            existing_keys.add(k)
            w.writerow(t.to_row())
            added += 1
    return added


def import_csv(path: str, dedupe: bool = True) -> List[TrackTask]:
    tasks: List[TrackTask] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        # accept flexible headers
        fields = [c.lower() for c in (reader.fieldnames or [])]
        def get(row, *names):
            for n in names:
                for k in row.keys():
                    if k and k.lower() == n:
                        return row.get(k) or ""
            return ""
        for row in reader:
            artist = (get(row, "artist", "artist name(s)", "artists", "singers") or "").strip()
            title = (get(row, "title", "track name", "song", "name", "track") or "").strip()
            album = (get(row, "album", "album name") or "").strip()
            secs_raw = get(row, "seconds", "duration (ms)", "duration_ms", "duration")
            secs = 0
            try:
                if "duration (ms)" in fields or "duration_ms" in fields or (secs_raw and float(secs_raw) > 10000):
                    secs = int(round(float(secs_raw) / 1000))
                else:
                    secs = int(float(secs_raw or 0))
            except Exception:
                secs = 0
            if not artist and not title:
                continue
            if ";" in artist:
                artist = ", ".join([a.strip() for a in artist.split(";") if a.strip()])
            tasks.append(TrackTask(artist=artist, title=title, album=album, seconds=secs))
    if dedupe:
        seen = set()
        uniq = []
        for t in tasks:
            k = (norm_text(t.artist), norm_text(t.title))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(t)
        tasks = uniq
    return tasks


def dedupe_tasks(tasks: List[TrackTask]) -> List[TrackTask]:
    seen = set()
    out = []
    for t in tasks:
        k = (norm_text(t.artist), norm_text(t.title))
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out
