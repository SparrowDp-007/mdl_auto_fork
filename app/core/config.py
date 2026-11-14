from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

from .models import AppSettings


def config_dir(app_name: str = "MusicDL-Fork") -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    p = os.path.join(base, app_name)
    os.makedirs(p, exist_ok=True)
    return p


def config_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def load_settings() -> AppSettings:
    defaults = AppSettings.defaults()
    path = config_path()
    if not os.path.exists(path):
        return defaults
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(defaults, k):
                setattr(defaults, k, v)
    except Exception:
        pass
    return defaults


def save_settings(settings: AppSettings) -> None:
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(settings), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
