#!/usr/bin/env python3
"""MusicDL Fork - точка запуска: проверка компонентов (с запросом разрешения) и старт GUI."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # bootstrap: check & ask permission only for missing components
    from app.bootstrap import ensure_ready
    ready = ensure_ready()
    if not ready:
        # user declined installation
        return 1

    # launch GUI
    from app.ui.main_window import App
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
