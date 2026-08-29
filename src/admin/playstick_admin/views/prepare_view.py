"""The Prepare tab: everything this machine can see, and what to do about it.

WHAT THIS TAB IS FOR, WHICH CHANGED

It used to be a list of films the preparer had not reached yet, and one button
that ran the whole library. That was the right shape when the library was one
directory and the only question was "has this been done". The host sees more
than the stick can hold now, so the question became "which of these do we want",
and a question like that needs a table you can sort, filter and select in.

So: sources at the top, everything they offered in the middle, and the queue at
the bottom. Adding to the library is a selection and a button, and what happens
after it is one film at a time, in order, survivable across a closed window.

WHAT THE TABLE SHOWS BEFORE ANYTHING HAS BEEN PROBED

Title, year and size, all of which come from the filename and a stat. That is
deliberate: probing four thousand files across three mounts is minutes, and a
tab that could not draw until it finished would be a tab nobody opens. Codecs
and duration fill in for a film once somebody has actually asked for it, which
is also the first moment anybody needs them.

THE QUEUE IS THE DATABASE, NOT THIS WIDGET

Every button here writes a row and asks for a redraw. Nothing about what has
been asked for lives in this file, which is why closing the window is a pause
rather than a loss.
"""

import os

from PySide6.QtCore import (QAbstractTableModel, QModelIndex,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QSplitter,
                               QTableView, QVBoxLayout, QWidget)

from . import format as fmt
from ..bridges import net_worker, queue_worker
from ..bridges.watcher import LibraryWatcher
from ..core import catalog, queue, scan

MEDIA_COLUMNS = ["Title", "Year", "Size", "Duration", "Video", "Source",
                 "Status", "Notes"]
QUEUE_COLUMNS = ["#", "Title", "State", "Tries", "Error"]

# What the Status column says, keyed by item.state. "in library" is a join
# rather than a state -- a film can be ready here and not yet on the stick.
STATUS = {
    catalog.AVAILABLE: "",
    catalog.QUEUED: "queued",
    catalog.PREPARING: "preparing",
    catalog.READY: "in library",
    catalog.FAILED: "failed",
    catalog.MISSING: "the file is gone",
}


def _video_str(row):
    if not row["height"]:
        return ""
    bits = ["%dp" % row["height"]]
    if row["vcodec"]:
        bits.append(row["vcodec"])
    return " ".join(bits)


def _duration_str(seconds):
    if not seconds:
        return ""
    total = int(seconds)
    return "%d:%02d" % (total // 3600, (total % 3600) // 60)


class MediaModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(MEDIA_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return MEDIA_COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ToolTipRole:
            return row["abs_path"]
        if role == Qt.UserRole:
            # What the proxy sorts on, so "1.5 GB" files in size order rather
            # than in alphabetical order of their own labels.
            return {2: row["size"] or 0, 3: row["duration"] or 0,
                    1: row["year"] or 0}.get(col)
        if role != Qt.DisplayRole:
            return None
        if col == 0:
            return row["title"] or row["rel"]
        if col == 1:
            return str(row["year"] or "")
        if col == 2:
            return fmt.human_size(row["size"])
        if col == 3:
            return _duration_str(row["duration"])
        if col == 4:
            return _video_str(row)
        if col == 5:
            return row["source_label"] or "(source removed)"
        if col == 6:
            if row["state"] == catalog.EXCLUDED:
                return row["excluded_reason"] or "excluded"
            if row["in_library"] and row["state"] == catalog.READY:
                return "in library" if row["pushed_at"] else "not on the stick"
            return STATUS.get(row["state"], row["state"])
        if col == 7:
            return row["excluded_reason"] or ""
        return None


class QueueModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(QUEUE_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return QUEUE_COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        row = self._rows[index.row()]
        return [str(index.row() + 1), row["title"] or row["item_id"],
                row["state"], str(row["attempts"]),
                row["error"] or ""][index.column()]


class PrepareTab(QWidget):
    # One film reached the stick. Kept under the old name so the main window's
    # wiring to the Library tab is unchanged.
    libraryChanged = Signal()
    queueChanged = Signal()

    def __init__(self, db, settings, parent=None):
        super().__init__(parent)
        self._db = db
        self._settings = settings
        self._worker = None
        self._thread = None
        self._threads = set()
        self._media = MediaModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._media)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)
        self._proxy.setSortRole(Qt.UserRole)
        self._queue = QueueModel()
        self._watcher = LibraryWatcher(parent=self)
        self._watcher.changed.connect(self.rescan)
        self._build()

    # --- building ----------------------------------------------------------

    def _build(self):
        outer = QVBoxLayout(self)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_catalogue())
        splitter.addWidget(self._build_queue())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

    def _build_catalogue(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        sources = QHBoxLayout()
        self._sources_label = QLabel("No sources yet.")
        add = QPushButton("Add source...")
        add.clicked.connect(self.add_source)
        self._remove_source_btn = QPushButton("Remove source...")
        self._remove_source_btn.clicked.connect(self.remove_source)
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self.rescan)
        sources.addWidget(self._sources_label, 1)
        sources.addWidget(add)
        sources.addWidget(self._remove_source_btn)
        sources.addWidget(rescan)
        layout.addLayout(sources)

        filters = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter by title, source, path...")
        self._filter.textChanged.connect(self._proxy.setFilterFixedString)
        self._hide_done = QCheckBox("Hide what is already in the library")
        self._hide_done.toggled.connect(self.reload)
        self._show_excluded = QCheckBox("Show excluded")
        self._show_excluded.toggled.connect(self.reload)
        filters.addWidget(self._filter, 1)
        filters.addWidget(self._hide_done)
        filters.addWidget(self._show_excluded)
        layout.addLayout(filters)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # The whole point of the tab: pick many, add them all at once.
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_menu)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self._table.selectionModel().selectionChanged.connect(self._on_select)
        layout.addWidget(self._table, 1)

        actions = QHBoxLayout()
        self._selection_label = QLabel("")
        self._add_btn = QPushButton("Add to library")
        self._add_btn.clicked.connect(self.add_to_library)
        self._add_btn.setEnabled(False)
        actions.addWidget(self._selection_label, 1)
        actions.addWidget(self._add_btn)
        layout.addLayout(actions)
        return box

    def _build_queue(self):
        box = QGroupBox("Preparing")
        layout = QVBoxLayout(box)

        self._queue_view = QTableView()
        self._queue_view.setModel(self._queue)
        self._queue_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._queue_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._queue_view.verticalHeader().setVisible(False)
        self._queue_view.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        layout.addWidget(self._queue_view, 1)

        row = QHBoxLayout()
        for label, slot in (("Move up", lambda: self._move(-1)),
                            ("Move down", lambda: self._move(1)),
                            ("Remove", self._remove_job),
                            ("Retry failed", self._retry_failed),
                            ("Clear finished", self._clear_finished)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        self._stop_btn = QPushButton("Stop after this film")
        self._stop_btn.clicked.connect(self._stop)
        self._stop_btn.setEnabled(False)
        self._cancel_btn = QPushButton("Cancel this film")
        self._cancel_btn.clicked.connect(self._cancel)
        self._cancel_btn.setEnabled(False)
        row.addWidget(self._stop_btn)
        row.addWidget(self._cancel_btn)
        layout.addLayout(row)

        self._current = QLabel("Nothing is being prepared.")
        self._progress = QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        layout.addWidget(self._current)
        layout.addWidget(self._progress)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(5000)
        layout.addWidget(self._log, 1)
        return box

    # --- loading -----------------------------------------------------------

    def reload(self):
        """Redraw from the database. Cheap, and the only way anything here
        changes: every button writes a row and calls this."""
        states = None
        if not self._show_excluded.isChecked():
            states = [catalog.AVAILABLE, catalog.QUEUED, catalog.PREPARING,
                      catalog.READY, catalog.FAILED, catalog.MISSING]
        rows = catalog.items(self._db, states=states)
        if self._hide_done.isChecked():
            rows = [r for r in rows if not r["in_library"]]
        self._media.set_rows(rows)
        self._queue.set_rows(queue.pending(self._db))
        self._draw_sources()
        self._on_select()
        self.queueChanged.emit()

    def _draw_sources(self):
        sources = catalog.sources(self._db)
        if not sources:
            self._sources_label.setText(
                "No sources yet -- add a folder of films to see them here.")
            self._remove_source_btn.setEnabled(False)
            return
        self._remove_source_btn.setEnabled(True)
        counts = catalog.counts(self._db)
        total = sum(counts.values())
        missing = [s for s in sources if not os.path.isdir(s["path"])]
        text = "%d source(s), %d film(s)" % (len(sources), total)
        if missing:
            text += " -- %d source(s) not reachable right now" % len(missing)
        self._sources_label.setText(text)

    def refresh_roots(self):
        self._watcher.set_roots(scan.roots(self._db))

    # --- sources -----------------------------------------------------------

    def add_source(self):
        path = QFileDialog.getExistingDirectory(self, "Add a source folder")
        if not path:
            return
        catalog.add_source(self._db, path)
        self.refresh_roots()
        self.rescan()

    def remove_source(self):
        sources = catalog.sources(self._db)
        if not sources:
            return
        menu = QMenu(self)
        actions = {menu.addAction(s["path"]): s for s in sources}
        chosen = menu.exec(self._remove_source_btn.mapToGlobal(
            self._remove_source_btn.rect().bottomLeft()))
        if chosen is None:
            return
        source = actions[chosen]
        answer = QMessageBox.question(
            self, "Remove this source?",
            "Stop scanning %s?\n\nFilms from it that are already in the "
            "library stay in the library -- this only stops looking there."
            % source["path"])
        if answer != QMessageBox.Yes:
            return
        catalog.remove_source(self._db, source["id"])
        self.refresh_roots()
        self.reload()

    def rescan(self):
        """Walk every source, in the background.

        On a worker thread because a walk of three mounts is seconds at best
        and a frozen window is indistinguishable from a crash.
        """
        problem = self._settings.nas_root_problem()
        if problem:
            self._log.appendPlainText("--- %s ---" % problem)
        job, thread = net_worker.call_with_progress(
            lambda say: scan.scan_all(self._db, self._settings.nas_root,
                                      self._settings.prep_extra_args, say))
        job.progress.connect(self._log.appendPlainText)
        job.done.connect(lambda summary: self.reload())
        job.failed.connect(
            lambda msg: self._log.appendPlainText("--- scan failed: %s ---" % msg))
        self._track(thread)
        thread.start()

    def _track(self, thread):
        self._threads.add(thread)
        thread.finished.connect(lambda: self._threads.discard(thread))

    # --- selection ---------------------------------------------------------

    def _selected(self):
        rows = self._table.selectionModel().selectedRows()
        out = []
        for index in rows:
            row = self._media.row_at(self._proxy.mapToSource(index).row())
            if row is not None:
                out.append(row)
        return out

    def _on_select(self):
        rows = self._selected()
        addable = [r for r in rows if r["state"] in
                   (catalog.AVAILABLE, catalog.FAILED, catalog.EXCLUDED)]
        self._add_btn.setEnabled(bool(addable))
        if not rows:
            self._selection_label.setText("")
            return
        total = sum(r["size"] or 0 for r in addable)
        already = len(rows) - len(addable)
        text = "%d selected, %d to add (%s)" % (
            len(rows), len(addable), fmt.human_size(total))
        if already:
            text += ", %d already queued or in the library" % already
        self._selection_label.setText(text)

    def _on_menu(self, point):
        rows = self._selected()
        if not rows:
            return
        menu = QMenu(self)
        add = menu.addAction("Add to library")
        exclude = menu.addAction("Exclude")
        include = menu.addAction("Put back")
        chosen = menu.exec(self._table.viewport().mapToGlobal(point))
        if chosen is add:
            self.add_to_library()
        elif chosen is exclude:
            for row in rows:
                catalog.set_state(self._db, row["id"], catalog.EXCLUDED,
                                  "excluded by hand")
            self.reload()
        elif chosen is include:
            for row in rows:
                if row["state"] == catalog.EXCLUDED:
                    catalog.set_state(self._db, row["id"], catalog.AVAILABLE)
            self.reload()

    # --- the queue ---------------------------------------------------------

    def add_to_library(self):
        problem = self._settings.nas_root_problem()
        if problem:
            QMessageBox.warning(self, "The share is not usable", problem)
            return
        rows = [r for r in self._selected() if r["state"] in
                (catalog.AVAILABLE, catalog.FAILED, catalog.EXCLUDED)]
        if not rows:
            return
        queue.enqueue_many(self._db, [r["id"] for r in rows])
        self._log.appendPlainText("--- queued %d film(s) ---" % len(rows))
        self.reload()
        self.start_queue()

    def enqueue_one(self, ident):
        """Ask for one film by id. The Library tab's "re-prepare" ends up
        here, which is what makes correcting a film a queued job rather than a
        whole-library run."""
        queue.enqueue(self._db, ident)
        self.reload()
        self.start_queue()

    def start_queue(self):
        """Start draining, unless something already is.

        The guard is here as well as in the database because starting a second
        thread would mean two objects installing themselves as the preparer's
        one reporter.
        """
        if self._thread is not None:
            return
        if not queue.pending(self._db):
            return
        worker, thread = queue_worker.start(self._db, self._settings)
        worker.jobStarted.connect(self._on_job_started)
        worker.jobPhase.connect(self._on_phase)
        worker.jobLog.connect(self._log.appendPlainText)
        worker.jobDone.connect(self._on_job_done)
        worker.jobFailed.connect(self._on_job_failed)
        worker.queueChanged.connect(self.reload)
        worker.finished.connect(self._on_queue_drained)
        self._worker, self._thread = worker, thread
        self._stop_btn.setEnabled(True)
        self._cancel_btn.setEnabled(True)
        self._progress.setRange(0, 0)          # indeterminate: it is one film
        thread.start()

    def recover(self):
        """Put an interrupted queue back in order, and say so. Called once at
        startup, before anything is drawn."""
        note = queue_worker.recover(self._db, self._settings)
        if note:
            self._log.appendPlainText("--- %s ---" % note)

    def _stop(self):
        if self._worker:
            self._worker.stop()
            self._current.setText("Stopping after this film...")
            self._stop_btn.setEnabled(False)

    def _cancel(self):
        if self._worker:
            self._worker.cancel_current()
            self._current.setText("Cancelling this film...")

    def _move(self, delta):
        row = self._queue_view.currentIndex().row()
        job = self._queue.row_at(row)
        if job is not None and queue.move(self._db, job["id"], delta):
            self.reload()

    def _remove_job(self):
        job = self._queue.row_at(self._queue_view.currentIndex().row())
        if job is None:
            return
        if not queue.remove(self._db, job["id"]):
            QMessageBox.information(
                self, "Already started",
                "That film is being prepared now. \"Cancel this film\" stops it.")
            return
        self.reload()

    def _retry_failed(self):
        failed = queue.recent_failures(self._db)
        if not failed:
            return
        queue.enqueue_many(self._db, [row["item_id"] for row in failed])
        self.reload()
        self.start_queue()

    def _clear_finished(self):
        queue.clear_finished(self._db)
        self.reload()

    # --- worker signals ----------------------------------------------------

    def _on_job_started(self, event):
        self._current.setText("%s -- %d left in the queue"
                              % (event["title"], event["remaining"]))
        self._log.appendPlainText("--- preparing %s ---" % event["title"])

    def _on_phase(self, phase, detail):
        head = self._current.text().split(" -- ")[0]
        self._current.setText("%s -- %s%s"
                              % (head, phase, (": " + detail) if detail else ""))

    def _on_job_done(self, result):
        self._log.appendPlainText(
            "--- %s: %s onto the share in %ss%s ---"
            % (result.get("title"), fmt.human_size(result.get("bytes")),
               result.get("seconds"),
               "" if result.get("pushed") else ", waiting for the stick"))
        if result.get("pushed"):
            self.libraryChanged.emit()

    def _on_job_failed(self, event):
        self._current.setText("%s failed: %s" % (event["title"], event["error"]))

    def _on_queue_drained(self):
        self._worker = None
        self._thread = None
        self._stop_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._progress.setRange(0, 1)
        self._progress.setValue(1)
        self._current.setText("Nothing is being prepared.")
        self.reload()
        # A drained queue usually means films arrived; and if the stick was off
        # for some of them, the Library tab should still be told to look again.
        self.libraryChanged.emit()
