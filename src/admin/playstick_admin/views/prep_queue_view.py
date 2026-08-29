"""The Prepare tab: what is waiting, a run in progress, and its log.

The preparer works the whole library and skips what its cache says is already
done, so "prepare" here means one run: the films listed as new are exactly the
ones that run will reach. New films are found on launch and while the app is
open, by watching the source library.

A run starts by pulling the device's metadata edits down into the preparer's
overrides file -- see _pull_device_edits. That is not a nicety: the device is
where an edit lives now, and a run that did not know about one would derive the
old title from the filename again and overwrite the correction at the next
sync.

WHAT A RUN DOES NOT DO is put anything on the device. It writes an index and a
tree of media on THIS machine; the Device tab's "Sync library to device" is
what carries it over. Two steps rather than one because they fail differently
-- a preparation is hours of ffmpeg and a sync is a few seconds of HTTP, and a
stick that is switched off should not be able to waste the first.
"""

import os
import shlex

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

from ..bridges import prep_worker
from ..bridges.watcher import LibraryWatcher
from ..core import sync
from ..core.device_client import DeviceClient, DeviceError
from .format import human_size


class PrepTab(QWidget):
    libraryChanged = Signal()       # a run finished; other tabs may want a reload

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._worker = None
        self._thread = None
        self._watcher = LibraryWatcher(parent=self)
        self._watcher.changed.connect(self.rescan)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)

        header = QHBoxLayout()
        self._new_label = QLabel("New films: -")
        rescan_btn = QPushButton("Rescan now")
        rescan_btn.clicked.connect(self.rescan)
        header.addWidget(self._new_label, 1)
        header.addWidget(rescan_btn)
        outer.addLayout(header)

        self._new_list = QListWidget()
        outer.addWidget(self._new_list, 1)

        options = QHBoxLayout()
        self._dry_run = QCheckBox("Dry run")
        self._refresh_posters = QCheckBox("Refresh posters")
        self._prepare_btn = QPushButton("Prepare library")
        self._prepare_btn.clicked.connect(self.prepare)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._cancel)
        self._cancel_btn.setEnabled(False)
        options.addWidget(self._dry_run)
        options.addWidget(self._refresh_posters)
        options.addStretch(1)
        options.addWidget(self._prepare_btn)
        options.addWidget(self._cancel_btn)
        outer.addLayout(options)

        self._progress = QProgressBar()
        self._progress.setFormat("%v / %m")
        self._current = QLabel("")
        outer.addWidget(self._current)
        outer.addWidget(self._progress)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(5000)
        outer.addWidget(self._log, 2)

    # --- scanning ----------------------------------------------------------

    def refresh_root(self):
        self._watcher.set_root(self._settings.source_library)
        self.rescan()

    def rescan(self):
        from ..core import library_model
        source = self._settings.source_library
        output = self._settings.effective_output()
        self._new_list.clear()
        if not source or not os.path.isdir(source):
            self._new_label.setText("New films: (set a source library in Settings)")
            return
        try:
            new = library_model.scan_new(source, output)
        except Exception as exc:                    # pragma: no cover - defensive
            self._new_label.setText("Scan failed: %s" % exc)
            return
        self._new_label.setText("New films: %d" % len(new))
        for f in new:
            item = QListWidgetItem("%s   (%s)" % (f["title"], human_size(f["size"])))
            item.setToolTip(f["rel"])
            item.setData(Qt.UserRole, f)
            self._new_list.addItem(item)

    # --- running -----------------------------------------------------------

    def _build_argv(self, extra=None):
        s = self._settings
        argv = ["--library", s.source_library]
        if s.output_dir:
            argv += ["--output", s.output_dir]
        if s.tmdb_key:
            argv += ["--tmdb-key", s.tmdb_key, "--tmdb-language", s.tmdb_language]
        if s.prep_extra_args:
            argv += shlex.split(s.prep_extra_args)
        if self._dry_run.isChecked():
            argv.append("--dry-run")
        if self._refresh_posters.isChecked():
            argv.append("--refresh-posters")
        if extra:
            argv += extra
        return argv

    def prepare(self, extra=None):
        if self._thread is not None:
            QMessageBox.information(self, "Busy", "A run is already in progress.")
            return
        source = self._settings.source_library
        if not source or not os.path.isdir(source):
            QMessageBox.warning(self, "No library",
                                "Set a valid source library in Settings.")
            return
        # Before anything else: bring down whatever anybody corrected ON the
        # device, so this run honours it instead of re-deriving the old answer
        # from the filename and pushing it back at the next sync. The device is
        # the one home for a metadata edit now, and this is the moment that
        # matters -- a run started without it is the exact way an edit gets
        # silently undone.
        #
        # Never fatal. A stick that is switched off is a reason to prepare
        # films with the overrides already on this machine, not a reason to
        # refuse to prepare them.
        self._pull_device_edits()

        argv = self._build_argv(extra if isinstance(extra, list) else None)
        self._log.appendPlainText("--- starting: playstick-prep %s ---"
                                  % " ".join(argv))
        self._progress.setRange(0, 0)           # indeterminate until run_start
        self._current.setText("Probing the library...")
        self._prepare_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        worker, thread = prep_worker.start_prep(
            argv, self._settings.ffmpeg, self._settings.ffprobe)
        worker.logLine.connect(self._on_log)
        worker.runStarted.connect(self._on_run_started)
        worker.filmStarted.connect(self._on_film_started)
        worker.filmDone.connect(self._on_film_done)
        worker.runFinished.connect(self._on_run_finished)
        worker.finished.connect(self._on_thread_done)
        self._worker = worker
        self._thread = thread
        thread.start()

    def _pull_device_edits(self):
        if not self._settings.device_url:
            return
        output = self._settings.effective_output()
        if not output:
            return
        try:
            changed = sync.pull_overrides(
                DeviceClient(self._settings.device_url), output)
        except DeviceError as exc:
            self._log.appendPlainText(
                "--- the device did not answer (%s); preparing with the "
                "overrides already here ---" % exc)
            return
        if changed:
            self._log.appendPlainText(
                "--- took %d edit(s) from the device ---" % changed)

    def reprepare(self, movie):
        """Re-run after a correction. There is no single-film mode in the
        preparer, so this is a normal run with posters refreshed -- the cache
        skips the encodes that are already done, and the corrected metadata and
        pinned match are re-applied to every film on the way past."""
        if self._thread is not None:
            QMessageBox.information(self, "Busy", "A run is already in progress.")
            return
        self._refresh_posters.setChecked(True)
        self._log.appendPlainText("--- re-preparing after correcting %s ---"
                                  % (movie.get("title") or movie.get("id")))
        self.prepare()

    def _cancel(self):
        if self._worker:
            self._worker.cancel()
            self._current.setText("Stopping after the current film...")

    # --- signal handlers ---------------------------------------------------

    def _on_log(self, text):
        self._log.appendPlainText(text)

    def _on_run_started(self, total):
        self._progress.setRange(0, max(total, 1))
        self._progress.setValue(0)

    def _on_film_started(self, ev):
        self._current.setText("[%s/%s] %s" % (ev.get("number"), ev.get("total"),
                                              ev.get("title")))

    def _on_film_done(self, ev):
        self._progress.setValue(int(ev.get("number", 0)))

    def _on_run_finished(self, summary):
        if summary.get("error"):
            self._current.setText("Failed: %s" % summary["error"])
        elif summary.get("stopped"):
            self._current.setText("Stopped after %s film(s)."
                                  % summary.get("prepared", "?"))
        else:
            self._current.setText(
                "Done: %s indexed, %s prepared, %s failed."
                % (summary.get("indexed", "?"), summary.get("prepared", "?"),
                   summary.get("failed", 0)))
        self._progress.setRange(0, 1)
        self._progress.setValue(1)

    def _on_thread_done(self):
        self._worker = None
        self._thread = None
        self._prepare_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self.rescan()
        self.libraryChanged.emit()
