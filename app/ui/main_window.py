from __future__ import annotations

import csv
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.config import load_settings, save_settings
from ..core.manager import DownloadManager
from ..core.models import AppSettings, ProgressEvent, TaskStatus, TrackTask, norm_text
from ..core.queue_store import append_tasks, ensure_queue_file, import_csv, read_queue, write_queue

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
QUALITY_OPTIONS = {
    "Лучшее (lossless→цель/320)": "best",
    "Высокое (320 kbps)": "high",
    "Среднее (192 kbps)": "medium",
    "Низкое (128 kbps)": "low",
    "Своё": "custom",
}
ALL_SOURCES = ["NeteaseMusicClient", "KuwoMusicClient", "BilibiliMusicClient", "YouTubeMusicClient"]

DARK_QSS = """
QWidget { background-color: #1e1e1e; color: #e6e6e6; font-size: 13px; }
QMainWindow, QDialog { background-color: #1e1e1e; }
QGroupBox { border: 1px solid #3a3a3a; border-radius: 6px; margin-top: 8px; padding-top: 8px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #d0d0d0; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox { background-color: #2b2b2b; border: 1px solid #444; border-radius: 4px; padding: 4px; selection-background-color: #3a6ea5; }
QLineEdit:read-only { color: #b8b8b8; }
QPushButton { background-color: #3a3a3a; border: 1px solid #555; border-radius: 4px; padding: 6px 10px; }
QPushButton:hover { background-color: #4a4a4a; }
QPushButton:pressed { background-color: #2f2f2f; }
QPushButton#startBtn { background-color: #2d7d46; border: 1px solid #25663a; }
QPushButton#startBtn:hover { background-color: #25663a; }
QPushButton#stopBtn { background-color: #8b2e2e; border: 1px solid #6f2424; }
QPushButton#stopBtn:hover { background-color: #6f2424; }
QPushButton#accentBtn { background-color: #5a4b8a; border: 1px solid #483c70; }
QPushButton#accentBtn:hover { background-color: #483c70; }
QProgressBar { border: 1px solid #444; border-radius: 4px; text-align: center; background-color: #2b2b2b; height: 16px; }
QProgressBar::chunk { background-color: #3a6ea5; border-radius: 3px; }
QLabel { background: transparent; }
QCheckBox { spacing: 6px; }
QScrollBar:vertical { background: #252525; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #555; min-height: 20px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: #666; }
QComboBox QAbstractItemView { background-color: #2b2b2b; selection-background-color: #3a6ea5; }
"""


def open_path(path: str):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        try:
            webbrowser.open(path)
        except Exception:
            pass


class SourcesDialog(QtWidgets.QDialog):
    def __init__(self, parent, selected):
        super().__init__(parent)
        self.setWindowTitle("Источники поиска")
        self.resize(420, 300)
        self.selected = list(selected)
        layout = QtWidgets.QVBoxLayout(self)
        self.checks = {}
        cur = set(selected or ALL_SOURCES)
        for s in ALL_SOURCES:
            cb = QtWidgets.QCheckBox(s)
            cb.setChecked(s in cur)
            layout.addWidget(cb)
            self.checks[s] = cb
        layout.addStretch(1)
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        ok = QtWidgets.QPushButton("Сохранить")
        ok.clicked.connect(self._save)
        btns.addWidget(ok)
        layout.addLayout(btns)

    def _save(self):
        sel = [s for s in ALL_SOURCES if self.checks[s].isChecked()]
        if not sel:
            QtWidgets.QMessageBox.warning(self, "Источники", "Нужен хотя бы один источник")
            return
        self.selected = sel
        self.accept()


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, settings: AppSettings):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.resize(520, 420)
        self.settings = settings
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.w_spin = QtWidgets.QSpinBox()
        self.w_spin.setRange(1, 6)
        self.w_spin.setValue(int(settings.workers or 3))
        self.t_spin = QtWidgets.QSpinBox()
        self.t_spin.setRange(20, 600)
        self.t_spin.setValue(int(settings.search_timeout_s or 80))
        self.try_spin = QtWidgets.QSpinBox()
        self.try_spin.setRange(1, 8)
        self.try_spin.setValue(int(settings.try_candidates or 3))
        self.br_spin = QtWidgets.QSpinBox()
        self.br_spin.setRange(64, 320)
        self.br_spin.setSingleStep(32)
        self.br_spin.setValue(int(settings.custom_bitrate_kbps or 320))
        form.addRow("Параллельных воркеров (1-6)", self.w_spin)
        form.addRow("Таймаут поиска, сек", self.t_spin)
        form.addRow("Кандидатов пробовать на трек", self.try_spin)
        form.addRow("Свой битрейт (kbps) для custom", self.br_spin)
        layout.addLayout(form)
        self.skip_cb = QtWidgets.QCheckBox("Пропускать уже существующие файлы")
        self.skip_cb.setChecked(bool(settings.skip_existing))
        self.m3u_cb = QtWidgets.QCheckBox("Писать M3U плейлист")
        self.m3u_cb.setChecked(bool(settings.write_m3u))
        self.rep_cb = QtWidgets.QCheckBox("Писать CSV отчёт")
        self.rep_cb.setChecked(bool(settings.write_report))
        self.auto_retry_end_cb = QtWidgets.QCheckBox("По завершении списка: повторить failed/miss и запустить снова")
        self.auto_retry_end_cb.setChecked(bool(getattr(settings, "auto_retry_missed_on_end", False)))
        layout.addWidget(self.skip_cb)
        layout.addWidget(self.m3u_cb)
        layout.addWidget(self.rep_cb)
        layout.addWidget(self.auto_retry_end_cb)
        layout.addStretch(1)
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        ok = QtWidgets.QPushButton("Сохранить")
        ok.clicked.connect(self._save)
        btns.addWidget(ok)
        layout.addLayout(btns)

    def _save(self):
        self.settings.workers = int(self.w_spin.value())
        self.settings.search_timeout_s = int(self.t_spin.value())
        self.settings.try_candidates = int(self.try_spin.value())
        self.settings.custom_bitrate_kbps = int(self.br_spin.value())
        self.settings.skip_existing = bool(self.skip_cb.isChecked())
        self.settings.write_m3u = bool(self.m3u_cb.isChecked())
        self.settings.write_report = bool(self.rep_cb.isChecked())
        self.settings.auto_retry_missed_on_end = bool(self.auto_retry_end_cb.isChecked())
        self.accept()


class ReadmeDialog(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Формат списка (README)")
        self.resize(640, 420)
        layout = QtWidgets.QVBoxLayout(self)
        tb = QtWidgets.QPlainTextEdit()
        tb.setPlainText(README_TEXT)
        tb.setReadOnly(True)
        layout.addWidget(tb)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.setWindowTitle("MusicDL Fork")
        self.resize(1480, 860)
        self.setMinimumSize(1280, 720)

        self.ui_queue: queue.Queue = queue.Queue()
        self.manager = DownloadManager(self.settings, sink=self._on_manager_event)
        self._last_success_ts: float = 0.0
        self._last_success_name: str = ""
        self._starting = False
        self._last_queue_reorder_ts = 0.0

        self._build_ui()
        self._load_initial_queue()

        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self._poll_ui_queue)
        self.poll_timer.start(150)
        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.timeout.connect(self._tick_status)
        self.tick_timer.start(1000)

    # ---------------- UI build
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # top controls
        top = QtWidgets.QFrame()
        top_l = QtWidgets.QGridLayout(top)
        top_l.setContentsMargins(8, 8, 8, 8)
        top_l.setHorizontalSpacing(6)
        top_l.setColumnStretch(1, 1)

        top_l.addWidget(QtWidgets.QLabel("Папка сохранения:"), 0, 0)
        self.out_edit = QtWidgets.QLineEdit(self.settings.output_dir)
        top_l.addWidget(self.out_edit, 0, 1)
        b_out = QtWidgets.QPushButton("Выбрать...")
        b_out.clicked.connect(self._choose_out)
        top_l.addWidget(b_out, 0, 2)
        b_open = QtWidgets.QPushButton("Открыть папку")
        b_open.clicked.connect(self._open_out_folder)
        top_l.addWidget(b_open, 0, 3)

        top_l.addWidget(QtWidgets.QLabel("Формат:"), 0, 4)
        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems(FORMAT_OPTIONS)
        if self.settings.target_format in FORMAT_OPTIONS:
            self.format_combo.setCurrentText(self.settings.target_format)
        top_l.addWidget(self.format_combo, 0, 5)
        top_l.addWidget(QtWidgets.QLabel("Качество:"), 0, 6)
        self.quality_combo = QtWidgets.QComboBox()
        self.quality_combo.addItems(list(QUALITY_OPTIONS.keys()))
        q_rev = {v: k for k, v in QUALITY_OPTIONS.items()}
        self.quality_combo.setCurrentText(q_rev.get(self.settings.quality_mode, "Высокое (320 kbps)"))
        self.quality_combo.setMinimumWidth(220)
        top_l.addWidget(self.quality_combo, 0, 7)

        top_l.addWidget(QtWidgets.QLabel("Файл очереди (активный):"), 1, 0)
        self.queue_edit = QtWidgets.QLineEdit(self.settings.queue_file)
        self.queue_edit.setReadOnly(True)
        top_l.addWidget(self.queue_edit, 1, 1)
        b_q = QtWidgets.QPushButton("Выбрать...")
        b_q.clicked.connect(self._choose_queue_file)
        top_l.addWidget(b_q, 1, 2)
        b_fmt = QtWidgets.QPushButton("Формат (README)")
        b_fmt.clicked.connect(self._show_readme)
        top_l.addWidget(b_fmt, 1, 3)

        top_l.addWidget(QtWidgets.QLabel("Рестарт при простое:"), 1, 4)
        self.stall_combo = QtWidgets.QComboBox()
        self.stall_combo.addItems(list(STALL_OPTIONS.keys()))
        stall_rev = {v: k for k, v in STALL_OPTIONS.items()}
        self.stall_combo.setCurrentText(stall_rev.get(int(self.settings.stall_restart_minutes), "20 минут"))
        top_l.addWidget(self.stall_combo, 1, 5)
        b_src = QtWidgets.QPushButton("Источники...")
        b_src.clicked.connect(self._sources_dialog)
        top_l.addWidget(b_src, 1, 6)
        b_set = QtWidgets.QPushButton("Настройки")
        b_set.clicked.connect(self._settings_dialog)
        top_l.addWidget(b_set, 1, 7)
        root.addWidget(top)

        # main split
        mid = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(mid, 1)

        # left panel
        left = QtWidgets.QWidget()
        left_l = QtWidgets.QVBoxLayout(left)
        left_l.setContentsMargins(4, 4, 4, 4)
        left_l.setSpacing(6)

        prog_box = QtWidgets.QFrame()
        prog_l = QtWidgets.QGridLayout(prog_box)
        prog_l.setContentsMargins(6, 6, 6, 6)
        prog_l.addWidget(QtWidgets.QLabel("Общий прогресс:"), 0, 0)
        self.overall_bar = QtWidgets.QProgressBar()
        self.overall_bar.setRange(0, 1000)
        self.overall_bar.setValue(0)
        self.overall_bar.setTextVisible(False)
        prog_l.addWidget(self.overall_bar, 0, 1)
        self.overall_label = QtWidgets.QLabel("0 / 0")
        prog_l.addWidget(self.overall_label, 0, 2)
        prog_l.setColumnStretch(1, 1)

        self.workers_frame = QtWidgets.QFrame()
        self.workers_layout = QtWidgets.QGridLayout(self.workers_frame)
        self.workers_layout.setContentsMargins(0, 4, 0, 0)
        self.workers_layout.setColumnStretch(1, 1)
        prog_l.addWidget(self.workers_frame, 1, 0, 1, 3)
        self.worker_labels = {}
        self.worker_bars = {}
        self.worker_pcts = {}
        self._ensure_worker_labels(1)
        left_l.addWidget(prog_box)

        qa = QtWidgets.QFrame()
        qa_l = QtWidgets.QHBoxLayout(qa)
        qa_l.setContentsMargins(4, 2, 4, 2)
        b_start = QtWidgets.QPushButton("Старт")
        b_start.setObjectName("startBtn")
        b_start.clicked.connect(self._start)
        b_pause = QtWidgets.QPushButton("Пауза")
        b_pause.clicked.connect(self._pause)
        b_resume = QtWidgets.QPushButton("Продолжить")
        b_resume.clicked.connect(self._resume)
        b_stop = QtWidgets.QPushButton("Стоп")
        b_stop.setObjectName("stopBtn")
        b_stop.clicked.connect(self._stop)
        b_retry = QtWidgets.QPushButton("Повторить failed/miss")
        b_retry.clicked.connect(self._retry_failed)
        b_export = QtWidgets.QPushButton("Экспорт отчёта/M3U")
        b_export.clicked.connect(self._export_report)
        for b in (b_start, b_pause, b_resume, b_stop):
            qa_l.addWidget(b)
        qa_l.addSpacing(8)
        qa_l.addWidget(b_retry)
        qa_l.addWidget(b_export)
        qa_l.addStretch(1)
        left_l.addWidget(qa)

        q_group = QtWidgets.QGroupBox("Очередь (активные сверху)")
        q_l = QtWidgets.QVBoxLayout(q_group)
        self.queue_text = QtWidgets.QPlainTextEdit()
        self.queue_text.setReadOnly(True)
        self.queue_text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        mono.setPointSize(10)
        self.queue_text.setFont(mono)
        q_l.addWidget(self.queue_text)
        left_l.addWidget(q_group, 1)
        mid.addWidget(left)

        # right panel
        right = QtWidgets.QWidget()
        right_l = QtWidgets.QVBoxLayout(right)
        right_l.setContentsMargins(4, 4, 4, 4)
        right_l.setSpacing(6)

        add_group = QtWidgets.QGroupBox("Добавить трек в очередь")
        add_l = QtWidgets.QGridLayout(add_group)
        self.e_artist = QtWidgets.QLineEdit()
        self.e_artist.setPlaceholderText("Artist")
        self.e_title = QtWidgets.QLineEdit()
        self.e_title.setPlaceholderText("Title")
        self.e_album = QtWidgets.QLineEdit()
        self.e_album.setPlaceholderText("Album")
        self.e_secs = QtWidgets.QLineEdit()
        self.e_secs.setPlaceholderText("Seconds")
        self.e_secs.setMaximumWidth(120)
        add_l.addWidget(QtWidgets.QLabel("Artist"), 0, 0)
        add_l.addWidget(self.e_artist, 0, 1)
        add_l.addWidget(QtWidgets.QLabel("Title"), 1, 0)
        add_l.addWidget(self.e_title, 1, 1)
        add_l.addWidget(QtWidgets.QLabel("Album"), 2, 0)
        add_l.addWidget(self.e_album, 2, 1)
        add_l.addWidget(QtWidgets.QLabel("Seconds"), 3, 0)
        add_l.addWidget(self.e_secs, 3, 1)
        abtn_w = QtWidgets.QWidget()
        abtn_l = QtWidgets.QHBoxLayout(abtn_w)
        abtn_l.setContentsMargins(0, 4, 0, 0)
        b_add = QtWidgets.QPushButton("Добавить в конец очереди")
        b_add.clicked.connect(self._add_to_queue)
        b_pri = QtWidgets.QPushButton("Найти сейчас (приоритет)")
        b_pri.setObjectName("accentBtn")
        b_pri.clicked.connect(lambda: self._find_now(single=False))
        b_one = QtWidgets.QPushButton("Найти только этот трек")
        b_one.setObjectName("accentBtn")
        b_one.clicked.connect(lambda: self._find_now(single=True))
        abtn_l.addWidget(b_add)
        abtn_l.addWidget(b_pri)
        abtn_l.addWidget(b_one)
        abtn_l.addStretch(1)
        add_l.addWidget(abtn_w, 4, 0, 1, 2)
        right_l.addWidget(add_group)

        tools_group = QtWidgets.QGroupBox("Инструменты")
        tools_l = QtWidgets.QGridLayout(tools_group)
        b_log = QtWidgets.QPushButton("Открыть лог")
        b_log.clicked.connect(self._open_log)
        b_imp = QtWidgets.QPushButton("Импорт CSV (с дедупликацией)")
        b_imp.clicked.connect(self._import_csv)
        tools_l.addWidget(b_log, 0, 0)
        tools_l.addWidget(b_imp, 0, 1)
        tools_l.setColumnStretch(0, 1)
        tools_l.setColumnStretch(1, 1)
        right_l.addWidget(tools_group)

        info_group = QtWidgets.QGroupBox("")
        info_l = QtWidgets.QGridLayout(info_group)
        info_l.addWidget(QtWidgets.QLabel("Последнее успешное скачивание (OK):"), 0, 0)
        self.last_dl_label = QtWidgets.QLabel("—")
        info_l.addWidget(self.last_dl_label, 0, 1)
        info_l.addWidget(QtWidgets.QLabel("Свободно на диске:"), 1, 0)
        self.disk_label = QtWidgets.QLabel("—")
        info_l.addWidget(self.disk_label, 1, 1)
        info_l.setColumnStretch(1, 1)
        right_l.addWidget(info_group)

        log_group = QtWidgets.QGroupBox("Журнал / статус")
        log_l = QtWidgets.QVBoxLayout(log_group)
        self.log_text = QtWidgets.QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(5000)
        log_l.addWidget(self.log_text)
        right_l.addWidget(log_group, 1)
        mid.addWidget(right)
        mid.setSizes([850, 620])

    # ---------------- helpers
    def _ensure_worker_labels(self, count: int):
        try:
            count = max(1, min(int(count or 1), 6))
        except Exception:
            count = 1
        for i in reversed(range(self.workers_layout.count())):
            item = self.workers_layout.takeAt(i)
            w = item.widget()
            if w:
                w.deleteLater()
        self.worker_labels = {}
        self.worker_bars = {}
        self.worker_pcts = {}
        for i in range(count):
            lbl = QtWidgets.QLabel(f"Воркер {i+1}: ожидание")
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            pct = QtWidgets.QLabel("0%")
            pct.setFixedWidth(42)
            pct.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.workers_layout.addWidget(lbl, i, 0)
            self.workers_layout.addWidget(bar, i, 1)
            self.workers_layout.addWidget(pct, i, 2)
            self.worker_labels[i] = lbl
            self.worker_bars[i] = bar
            self.worker_pcts[i] = pct

    def _set_worker_status(self, wid, text: str, progress: float | None = None):
        try:
            wid = int(wid)
        except Exception:
            return
        if wid not in self.worker_labels:
            self._ensure_worker_labels(max(len(self.worker_labels), wid + 1))
        lbl = self.worker_labels.get(wid)
        if lbl:
            lbl.setText(f"Воркер {wid+1}: {text}")
        if progress is not None:
            try:
                p = max(0.0, min(1.0, float(progress)))
                bar = self.worker_bars.get(wid)
                pct = self.worker_pcts.get(wid)
                if bar:
                    bar.setValue(int(p * 100))
                if pct:
                    pct.setText(f"{int(p*100)}%")
            except Exception:
                pass

    def _apply_settings_from_ui(self):
        self.settings.output_dir = self.out_edit.text().strip() or self.settings.output_dir
        self.settings.queue_file = self.queue_edit.text().strip() or self.settings.queue_file
        self.settings.target_format = self.format_combo.currentText()
        self.settings.quality_mode = QUALITY_OPTIONS.get(self.quality_combo.currentText(), "high")
        self.settings.stall_restart_minutes = STALL_OPTIONS.get(self.stall_combo.currentText(), 20)
        os.makedirs(self.settings.output_dir, exist_ok=True)
        ensure_queue_file(self.settings.queue_file)
        self.manager.settings = self.settings
        save_settings(self.settings)

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.appendPlainText(f"[{ts}] {msg}")

    def _load_initial_queue(self):
        ensure_queue_file(self.settings.queue_file)
        self.manager.load_queue(self.settings.queue_file)
        self._refresh_queue_view()

    def _queue_sort_key(self, t: "TrackTask"):
        active = {TaskStatus.SEARCHING, TaskStatus.SELECTING, TaskStatus.DOWNLOADING, TaskStatus.CONVERTING, TaskStatus.TAGGING}
        if t.status in active:
            return (0, t.status.value)
        if t.status == TaskStatus.QUEUED:
            return (1, 0)
        if t.status == TaskStatus.PENDING:
            return (2, 0)
        if t.status in (TaskStatus.FAILED, TaskStatus.MISS):
            return (3, 0)
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
        with self.manager._lock:
            tasks = list(self.manager.tasks)
        try:
            tasks = sorted(tasks, key=self._queue_sort_key)
        except Exception:
            pass
        self._queue_tasks_snapshot = tasks
        self.overall_label.setText(f"{self.manager._done_count} / {len(tasks)}")
        total = max(1, len(tasks))
        self.overall_bar.setValue(int(1000 * self.manager._done_count / total))
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
        self.queue_text.setPlainText(text)
        self._last_queue_reorder_ts = time.time()

    def _tick_status(self):
        try:
            ts = self._last_success_ts or getattr(self.manager, "last_success_ts", 0) or 0
            if ts and ts > 0:
                tstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                name = getattr(self, "_last_success_name", "") or ""
                self.last_dl_label.setText(f"{tstr}  —  {name}" if name else tstr)
            else:
                self.last_dl_label.setText("—")
            out = self.out_edit.text().strip() or self.settings.output_dir
            if out:
                try:
                    root = out if os.path.exists(out) else (os.path.splitdrive(out)[0] + "\\")
                    if os.path.exists(root):
                        usage = shutil.disk_usage(root)
                        self.disk_label.setText(f"{usage.free/2**30:.1f} ГБ свободно")
                except Exception:
                    pass
        except Exception:
            pass

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

    def _handle_event(self, ev: ProgressEvent):
        if ev.kind == "log":
            self._append_log(ev.message)
        elif ev.kind == "refresh_queue":
            self._refresh_queue_view()
        elif ev.kind == "stall":
            self._append_log(f"[STALL] {ev.message}")
        elif ev.kind == "overall":
            d = ev.overall_done or 0
            t = ev.overall_total or 0
            self.overall_label.setText(f"{d} / {t}")
            self.overall_bar.setValue(int(1000 * d / t) if t else 0)
            if ev.message:
                self._append_log(ev.message)
            msg = ev.message or ""
            if "завершено/остановлено" in msg and not self.manager._stop.is_set():
                if getattr(self.settings, "auto_retry_missed_on_end", False):
                    with self.manager._lock:
                        has_retryable = any(tt.status in (TaskStatus.FAILED, TaskStatus.MISS) for tt in self.manager.tasks)
                    if has_retryable and not self.manager.is_running and not self._starting:
                        self._append_log("авто: конец списка — повтор failed/miss и запуск")
                        QtCore.QTimer.singleShot(800, self._auto_retry_and_start)
        elif ev.kind == "scan_progress":
            data = ev.data or {}
            cur = data.get("current", 0)
            tot = data.get("total", 0)
            sk = data.get("skipped", 0)
            prog = (float(cur) / float(tot)) if tot else None
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
            name = uid
            with self.manager._lock:
                for t in self.manager.tasks:
                    if t.uid == uid:
                        name = t.display_name
                        break
            if wid is not None:
                short = phase or status or ""
                self._set_worker_status(wid, f"{name} — {short}"[:120], progress=prog)
            if wid is not None and status in (TaskStatus.OK.value, TaskStatus.SKIPPED.value, TaskStatus.MISS.value, TaskStatus.FAILED.value, TaskStatus.CANCELED.value):
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
            with self.manager._lock:
                done = sum(1 for t in self.manager.tasks if t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED))
                total = len(self.manager.tasks)
            self.overall_label.setText(f"{done} / {total}")
            self.overall_bar.setValue(int(1000 * done / total) if total else 0)
            active_statuses = {TaskStatus.SEARCHING.value, TaskStatus.SELECTING.value, TaskStatus.DOWNLOADING.value, TaskStatus.CONVERTING.value, TaskStatus.TAGGING.value}
            if status == TaskStatus.OK.value or status in active_statuses or phase in ("старт",):
                now = time.time()
                if now - getattr(self, "_last_queue_reorder_ts", 0) > 2.0:
                    self._last_queue_reorder_ts = now
                    self._refresh_queue_view()

    # ---------------- actions
    def _choose_out(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Выбрать папку сохранения", self.out_edit.text() or os.path.expanduser("~"))
        if d:
            self.out_edit.setText(d)
            if not self.queue_edit.text().strip():
                self.queue_edit.setText(os.path.join(d, "queue.csv"))

    def _choose_queue_file(self):
        initial = self.queue_edit.text().strip() or os.path.join(self.out_edit.text().strip() or self.settings.output_dir, "queue.csv")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Выбрать или создать файл очереди (CSV)",
            initial,
            "CSV queue (*.csv);;All files (*.*)",
        )
        if not path:
            return
        self.queue_edit.setText(path)
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
        dlg = SourcesDialog(self, self.settings.sources or ALL_SOURCES)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.settings.sources = dlg.selected
            save_settings(self.settings)
            self.manager.settings = self.settings

    def _settings_dialog(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            save_settings(self.settings)
            self.manager.settings = self.settings

    def _show_readme(self):
        dlg = ReadmeDialog(self)
        dlg.exec()

    def _import_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Выбрать CSV для импорта в текущую очередь", "", "CSV (*.csv);;All files (*.*)")
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
            QtWidgets.QMessageBox.critical(self, "Импорт", str(e))

    def _add_to_queue(self):
        self._apply_settings_from_ui()
        artist = self.e_artist.text().strip()
        title = self.e_title.text().strip()
        album = self.e_album.text().strip()
        try:
            secs = int(float(self.e_secs.text().strip() or 0))
        except Exception:
            secs = 0
        if not artist and not title:
            QtWidgets.QMessageBox.warning(self, "Добавление", "Укажите хотя бы Artist или Title")
            return
        t = TrackTask(artist=artist, title=title, album=album, seconds=secs)
        self.manager.add_task(t, to_front=False)
        self.manager.load_queue(self.settings.queue_file)
        self._refresh_queue_view()
        self._append_log(f"добавлен в очередь: {t.display_name}")

    def _find_now(self, single: bool):
        self._apply_settings_from_ui()
        artist = self.e_artist.text().strip()
        title = self.e_title.text().strip()
        album = self.e_album.text().strip()
        try:
            secs = int(float(self.e_secs.text().strip() or 0))
        except Exception:
            secs = 0
        if not artist and not title:
            QtWidgets.QMessageBox.warning(self, "Поиск", "Укажите хотя бы Artist или Title")
            return
        t = TrackTask(artist=artist, title=title, album=album, seconds=secs)
        with self.manager._lock:
            existing = None
            for x in self.manager.tasks:
                if norm_text(x.artist) == norm_text(artist) and norm_text(x.title) == norm_text(title):
                    existing = x
                    break
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
        if not self.manager.tasks:
            self.manager.load_queue(self.settings.queue_file)
            self._refresh_queue_view()
        if not self.manager.tasks:
            QtWidgets.QMessageBox.information(self, "Старт", "Очередь пуста. Импортируйте CSV или добавьте треки.")
            return
        if all(t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED) for t in self.manager.tasks):
            QtWidgets.QMessageBox.information(self, "Старт", "Все задачи уже обработаны. Используйте «Повторить failed/miss» для повторной попытки нужных треков.")
            return
        if self.manager.is_running or self._starting:
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
            self.manager._done_count = sum(1 for t in self.manager.tasks if t.status in (TaskStatus.OK, TaskStatus.SKIPPED, TaskStatus.MISS, TaskStatus.FAILED, TaskStatus.CANCELED))
        self._refresh_queue_view()
        self._append_log(f"сброшено для повтора: {n}")

    def _auto_retry_and_start(self):
        try:
            if not getattr(self.settings, "auto_retry_missed_on_end", False):
                return
            if self.manager.is_running or self._starting:
                return
            with self.manager._lock:
                has_retryable = any(t.status in (TaskStatus.FAILED, TaskStatus.MISS) for t in self.manager.tasks)
            if not has_retryable:
                return
            self._retry_failed()
            self._start()
        except Exception as e:
            self._append_log(f"ошибка авто-повтора: {e}")

    def _open_log(self):
        candidates = [
            os.path.join(self.settings.output_dir, "_jobs", "results.jsonl"),
            os.path.join(self.settings.output_dir, "app.log"),
        ]
        for p in candidates:
            if os.path.exists(p):
                open_path(p)
                return
        p = os.path.join(self.settings.output_dir, "app.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write("")
        open_path(p)

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
            w.writerow(["Artist", "Title", "Album", "Seconds", "Status", "Source", "Quality", "Match", "DurDelta_s", "OutFile", "Note"])
            for t in tasks:
                w.writerow([t.artist, t.title, t.album, t.seconds, t.status.value, t.source, t.quality_label, t.match, t.dur_delta_s, t.out_file, t.note])
        if self.settings.write_m3u:
            lines = ["#EXTM3U"]
            for t in tasks:
                if t.status == TaskStatus.OK and t.out_file and os.path.exists(t.out_file):
                    lines.append(f"#EXTINF:{t.seconds},{os.path.splitext(os.path.basename(t.out_file))[0]}")
                    lines.append(os.path.basename(t.out_file))
            with open(m3u, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        self._append_log(f"экспорт: {rep}" + (f", {m3u}" if self.settings.write_m3u else ""))
        QtWidgets.QMessageBox.information(self, "Экспорт", f"Сохранено:\n{rep}" + (f"\n{m3u}" if self.settings.write_m3u else ""))

    def closeEvent(self, event: QtGui.QCloseEvent):
        try:
            self._apply_settings_from_ui()
            self.manager.save_queue(self.settings.queue_file)
            self.manager.stop()
        except Exception:
            pass
        event.accept()


class App:
    """Compatibility wrapper so existing entrypoints (launcher/app.main) keep working."""
    def __init__(self):
        self._app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._app.setStyleSheet(DARK_QSS)
        self._win = MainWindow()

    def mainloop(self):
        self._win.show()
        self._app.exec()


def main():
    app = App()
    app.mainloop()
