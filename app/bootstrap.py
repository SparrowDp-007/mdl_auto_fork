from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THIRD_PARTY_MUSICDL = ROOT / "third_party" / "musicdl"
MARKER = ROOT / ".setup_complete"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _musicdl_ok() -> bool:
    # must be importable; prefer/accept our bundled one installed
    try:
        import musicdl  # noqa
        return True
    except Exception:
        return False


def check_missing() -> list[str]:
    missing = []
    # core/runtime deps
    checks = [
        ("customtkinter", "customtkinter"),
        ("PIL", "Pillow"),
        ("mutagen", "mutagen"),
        ("av", "av"),
        ("requests", "requests"),
        ("numpy", "numpy"),
        ("certifi", "certifi"),
    ]
    for mod, pkg in checks:
        if not _has_module(mod):
            missing.append(pkg)
    # musicdl from bundled source
    if not _musicdl_ok():
        missing.append("musicdl (из bundled third_party/musicdl, исходники GitHub)")
    # de-dupe preserving order
    seen = set()
    out = []
    for m in missing:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def status_report() -> tuple[list[str], list[str]]:
    """Return (installed_lines, missing_lines) for user-facing report."""
    installed = []
    missing = []
    checks = [
        ("customtkinter", "customtkinter"),
        ("PIL", "Pillow"),
        ("mutagen", "mutagen"),
        ("av", "av (PyAV)"),
        ("requests", "requests"),
        ("numpy", "numpy"),
        ("certifi", "certifi"),
    ]
    for mod, label in checks:
        (installed if _has_module(mod) else missing).append(label)
    # musicdl
    if _musicdl_ok():
        try:
            import musicdl
            ver = getattr(musicdl, "__version__", "?")
            loc = getattr(musicdl, "__file__", "")
            installed.append(f"musicdl {ver} ({loc})")
        except Exception:
            installed.append("musicdl (установлен)")
    else:
        missing.append(f"musicdl (будет установлен из {THIRD_PARTY_MUSICDL})")
    return installed, missing


def _ask_permission_console(missing: list[str], installed: list[str]) -> bool:
    print("\n=== MusicDL Fork — проверка компонентов ===")
    if installed:
        print("Уже установлено (пропускаем):")
        for m in installed:
            print(f"  [OK] {m}")
    if not missing:
        print("\nВсе необходимые компоненты уже установлены.")
        return True
    print("\nНужно установить (требуется разрешение):")
    for m in missing:
        print(f"  [..] {m}")
    print(f"\nПапка проекта: {ROOT}")
    if any("musicdl" in m for m in missing):
        print(f"musicdl будет установлен из bundled-исходников: {THIRD_PARTY_MUSICDL}")
    print("Будут выполнены команды pip (может потребоваться интернет для загрузки пакетов с PyPI).")
    ans = input("\nРазрешить установку недостающих компонентов? [y/N]: ").strip().lower()
    return ans in ("y", "yes", "д", "да")


def _ask_permission_tk(missing: list[str], installed: list[str]) -> bool | None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        lines = []
        if installed:
            lines.append("Уже установлено (запрос не нужен):")
            for m in installed:
                lines.append(f"  ✓ {m}")
            lines.append("")
        if not missing:
            messagebox.showinfo("MusicDL Fork — проверка компонентов", "\n".join(lines) + "\nВсе необходимое уже установлено.", parent=root)
            root.destroy()
            return True
        lines.append("Нужно установить (запрашивается разрешение):")
        for m in missing:
            lines.append(f"  • {m}")
        if any("musicdl" in m for m in missing):
            lines.append(f"\nmusicdl — из bundled-исходников:\n{THIRD_PARTY_MUSICDL}")
        lines.append("\n(зависимости могут загружаться через pip/PyPI; нужен интернет).")
        lines.append("\nРазрешить установку только недостающих компонентов?")
        res = messagebox.askyesno("MusicDL Fork — разрешение на установку", "\n".join(lines), parent=root)
        root.destroy()
        return bool(res)
    except Exception:
        return None


def ask_permission(missing: list[str], installed: list[str]) -> bool:
    # prefer GUI prompt if possible
    res = _ask_permission_tk(missing, installed)
    if res is not None:
        return res
    return _ask_permission_console(missing, installed)


def _run_pip(args: list[str]) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", "pip"] + args
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        return p.returncode == 0, out.strip()
    except Exception as e:
        return False, str(e)


def install_missing(missing: list[str], log_cb=None) -> bool:
    def log(msg):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass
        else:
            print(msg)

    ok_all = True
    # 1) install bundled musicdl from local third_party source
    if any(m.startswith("musicdl") for m in missing):
        if THIRD_PARTY_MUSICDL.exists():
            log(f"Установка musicdl из {THIRD_PARTY_MUSICDL} ...")
            ok, out = _run_pip(["install", "--upgrade", str(THIRD_PARTY_MUSICDL)])
            log(out[-2000:] if out else ("ok" if ok else "ошибка"))
            if not ok:
                # fallback: try from GitHub URL? but user asked to add to project and install with permission;
                # we have it bundled - report failure
                ok_all = False
        else:
            log(f"ОШИБКА: не найден {THIRD_PARTY_MUSICDL}")
            ok_all = False

    # 2) install other python deps
    req = ROOT / "requirements.txt"
    pkgs = [m for m in missing if not m.startswith("musicdl")]
    # filter out ones that might be satisfied now
    remaining = []
    modmap = {"Pillow": "PIL", "customtkinter": "customtkinter", "mutagen": "mutagen", "av": "av", "requests": "requests", "numpy": "numpy", "certifi": "certifi"}
    for p in pkgs:
        mod = modmap.get(p, p)
        if not _has_module(mod):
            remaining.append(p)
    if remaining:
        log("Установка зависимостей: " + ", ".join(remaining))
        # try installing from requirements for consistency, else individual
        if req.exists():
            ok, out = _run_pip(["install", "-r", str(req)])
        else:
            ok, out = _run_pip(["install"] + remaining)
        log(out[-2000:] if out else ("ok" if ok else "ошибка"))
        if not ok:
            ok_all = False
    return ok_all


def ensure_ready() -> bool:
    """Check deps; on first/missing ask permission and install. Return True if ready to run app."""
    installed, missing = status_report()
    if not missing:
        try:
            MARKER.write_text("ok", encoding="utf-8")
        except Exception:
            pass
        return True
    allowed = ask_permission(missing, installed)
    if not allowed:
        return False
    install_missing(missing)
    # recheck
    _, still = status_report()
    if still:
        msg = "Не все компоненты установлены:\n" + "\n".join(f"• {s}" for s in still)
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            messagebox.showwarning("MusicDL Fork — установка", msg + "\n\nПриложение попробует запуститься, но возможны ошибки.", parent=r)
            r.destroy()
        except Exception:
            print(msg)
    try:
        if not still:
            MARKER.write_text("ok", encoding="utf-8")
    except Exception:
        pass
    return True  # try to run anyway if user allowed
