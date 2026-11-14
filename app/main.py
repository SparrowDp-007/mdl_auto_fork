import os
import sys


def main():
    # ensure project root on path when run as script
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # go through bootstrap (checks deps, asks permission only for missing)
    try:
        from app.bootstrap import ensure_ready
        if not ensure_ready():
            return
    except Exception:
        pass
    from app.ui.main_window import App
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

