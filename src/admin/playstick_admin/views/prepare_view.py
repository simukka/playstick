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

from PySide6.QtCore import (QAbstractTableModel, QItemSelectionModel,
                            QModelIndex, QSortFilterProxyModel, Qt, Signal)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QFileDialog, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTableView, QToolButton, QVBoxLayout,
                               QWidget)

from . import format as fmt
from .film_details import FilmDetailsDialog
from .table_delegates import (ID_ROLE, KIND_ROLE, ROW_HEIGHT, CheckDelegate,
                              DashDelegate, IconDelegate, PillDelegate,
                              PosterThumbs, StateDelegate, TitleDelegate,
                              draw_check)
from .. import icons, theme
from ..bridges import net_worker, queue_worker
from ..bridges.watcher import LibraryWatcher
from ..core import catalog, queue, scan

# The first column is the selection checkbox and the last the row's menu; both
# are drawn by delegates and have no text of their own.
MEDIA_COLUMNS = ["", "Title", "Year", "Size", "Duration", "Video", "Source",
                 "Status", "Notes", ""]
CHECK_COL, TITLE_COL, MENU_COL = 0, 1, len(MEDIA_COLUMNS) - 1
SOURCE_COL, STATUS_COL, NOTES_COL = 6, 7, 8
QUEUE_COLUMNS = ["#", "Title", "State", "Tries", "Error", ""]
REMOVE_COL = len(QUEUE_COLUMNS) - 1

# What the Status column says, keyed by item.state. "in library" is a join
# rather than a state -- a film can be ready here and not yet on the stick.
STATUS = {
    catalog.AVAILABLE: "",
    catalog.QUEUED: "Queued",
    catalog.PREPARING: "Preparing",
    catalog.READY: "In library",
    catalog.FAILED: "Failed",
}

# And the colour of its pill: see theme.PILLS.
STATUS_KIND = {
    catalog.QUEUED: "busy",
    catalog.PREPARING: "busy",
    catalog.READY: "ok",
    catalog.FAILED: "bad",
    catalog.EXCLUDED: "muted",
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


def _status(row):
    """(text, pill kind) for the Status column."""
    if row["state"] == catalog.EXCLUDED:
        return (row["excluded_reason"] or "excluded").capitalize(), "muted"
    if row["in_library"] and row["state"] == catalog.READY:
        return (("In library", "ok") if row["pushed_at"]
                else ("Not on the stick", "warn"))
    return STATUS.get(row["state"], row["state"]), \
        STATUS_KIND.get(row["state"], "muted")


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
        if role == ID_ROLE:
            return row["id"]
        if role == KIND_ROLE:
            if col == SOURCE_COL:
                # Two colours, alternating by source, so the films of two
                # sources side by side can be told apart at a glance.
                return "source-alt" if (row["source_id"] or 0) % 2 == 0 \
                    else "source"
            if col == STATUS_COL:
                return _status(row)[1]
            return None
        if role == Qt.UserRole:
            # What the proxy sorts on, so "1.5 GB" files in size order rather
            # than in alphabetical order of their own labels.
            return {TITLE_COL: (row["title"] or row["rel"] or "").lower(),
                    2: row["year"] or 0, 3: row["size"] or 0,
                    4: row["duration"] or 0}.get(col)
        if role != Qt.DisplayRole:
            return None
        if col == TITLE_COL:
            return row["title"] or row["rel"]
        if col == 2:
            return str(row["year"] or "")
        if col == 3:
            return fmt.human_size(row["size"])
        if col == 4:
            return _duration_str(row["duration"])
        if col == 5:
            return _video_str(row)
        if col == SOURCE_COL:
            return row["source_label"] or "(source removed)"
        if col == STATUS_COL:
            return _status(row)[0]
        if col == NOTES_COL:
            return row["excluded_reason"] or ""
        return None


class QueueModel(QAbstractTableModel):
    """The waiting and running jobs, in order, and then each film's last
    failure that nothing has dealt with yet -- so that a failure can be
    retried or dismissed from the row it is on."""

    def __init__(self):
        super().__init__()
        self._rows = []
        # Whether the running job is held; the view knows, the table does not.
        self.paused = False

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rows(self):
        return list(self._rows)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(QUEUE_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return QUEUE_COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        state = row["state"]
        held = state == queue.RUNNING and self.paused
        if role == ID_ROLE:
            return row["item_id"]
        if role == KIND_ROLE and index.column() == 2:
            if held:
                return "pause"
            return {queue.RUNNING: "running",
                    queue.FAILED: "close"}.get(state, "clock")
        if role == Qt.TextAlignmentRole and index.column() in (0, 3):
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole and index.column() == 4:
            return row["error"] or None
        if role != Qt.DisplayRole:
            return None
        # Only work still to do has a place in the order.
        number = "" if state == queue.FAILED else str(index.row() + 1)
        return [number, row["title"] or row["item_id"],
                "paused" if held else state, str(row["attempts"]),
                row["error"] or "", ""][index.column()]


class CheckHeader(QHeaderView):
    """The media table's header, with a select-all box over the checkbox
    column."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setSectionsClickable(True)
        self.all_checked = False

    def paintSection(self, painter, rect, index):
        super().paintSection(painter, rect, index)
        if index == CHECK_COL:
            draw_check(painter, rect, self.all_checked)


class CheckableTable(QTableView):
    """A click on the checkbox column toggles that row in the selection,
    leaving the rest selected -- the checkbox is what it looks like. A click on
    the last column opens the row's menu."""

    menuRequested = Signal(object)          # a viewport point

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and event.button() == Qt.LeftButton:
            if index.column() == CHECK_COL:
                self.selectionModel().select(
                    index, QItemSelectionModel.Toggle | QItemSelectionModel.Rows)
                return
            if index.column() == MENU_COL:
                if not self.selectionModel().isRowSelected(index.row()):
                    self.selectRow(index.row())
                self.menuRequested.emit(event.position().toPoint())
                return
        super().mousePressEvent(event)


class LogConsole(QFrame):
    """The preparer's log: copy it, hold it still while reading, clear it.

    Held rather than stopped: lines that arrive while paused are kept and
    shown on resume, because an encode does not wait for anybody to finish
    reading, and a log with a hole in it is worse than one that scrolled.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("console")
        self._held = []
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 8, 8)
        row.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(icons.pixmap("terminal", theme.MUTED, 16))
        mark.setAlignment(Qt.AlignTop)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        tools = QVBoxLayout()
        tools.setSpacing(4)
        bar = QHBoxLayout()
        bar.setSpacing(2)
        self._pause = self._tool("pause", "Hold the log still", bar)
        self._pause.setCheckable(True)
        self._pause.toggled.connect(self._on_pause)
        self._tool("copy", "Copy the whole log", bar).clicked.connect(
            lambda: QApplication.clipboard().setText(self._text.toPlainText()))
        self._tool("trash", "Clear the log", bar).clicked.connect(self.clear)
        tools.addLayout(bar)
        tools.addStretch(1)
        row.addWidget(mark)
        row.addWidget(self._text, 1)
        row.addLayout(tools)

    @staticmethod
    def _tool(name, tip, bar):
        button = QToolButton()
        button.setObjectName("flat")
        button.setIcon(icons.icon(name, theme.MUTED, size=16))
        button.setToolTip(tip)
        bar.addWidget(button)
        return button

    def _on_pause(self, held):
        self._pause.setIcon(icons.icon("play" if held else "pause",
                                       theme.MUTED, size=16))
        self._pause.setToolTip("Carry on" if held else "Hold the log still")
        if not held:
            for line in self._held:
                self._text.appendPlainText(line)
            self._held = []

    def setMaximumBlockCount(self, count):
        self._text.setMaximumBlockCount(count)

    def appendPlainText(self, line):
        if self._pause.isChecked():
            self._held.append(line)
        else:
            self._text.appendPlainText(line)

    def clear(self):
        self._held = []
        self._text.clear()


class PrepareTab(QWidget):
    # One film reached the stick. Kept under the old name so the main window's
    # wiring to the Library tab is unchanged.
    libraryChanged = Signal()
    queueChanged = Signal()
    # "2 source(s), 30 film(s)", for the sidebar's status card.
    summaryChanged = Signal(str)

    def __init__(self, db, settings, monitor, parent=None):
        super().__init__(parent)
        self._db = db
        self._settings = settings
        self._monitor = monitor
        self._pushing = False
        self._job_line = ["", "", ""]
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
        outer.setContentsMargins(0, 0, 0, 0)
        self.header_actions = self._build_actions()
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(14)
        splitter.addWidget(self._build_catalogue())
        splitter.addWidget(self._build_queue())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        # The films get the larger share to start with; the handle moves it.
        splitter.setSizes([470, 530])
        outer.addWidget(splitter, 1)

    def _build_actions(self):
        """The source buttons, which the main window puts in the page's
        header rather than over the table."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        add = QPushButton("Add source...")
        add.setObjectName("primary")
        add.setIcon(icons.icon("plus", "#FFFFFF"))
        add.clicked.connect(self.add_source)
        rescan = QPushButton("Rescan")
        rescan.setObjectName("accent")
        rescan.setIcon(icons.icon("refresh", "#FFFFFF"))
        rescan.clicked.connect(self.rescan)
        for button in (add, rescan):
            row.addWidget(button)
        return box

    def _build_catalogue(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        filters = QHBoxLayout()
        filters.setSpacing(18)
        self._filter = QLineEdit()
        self._filter.setObjectName("search")
        self._filter.setPlaceholderText("Filter by title, source, path...")
        self._filter.addAction(icons.icon("search", theme.MUTED),
                               QLineEdit.LeadingPosition)
        self._filter.textChanged.connect(self._proxy.setFilterFixedString)
        self._hide_done = QCheckBox("Hide what is already in the library")
        self._hide_done.toggled.connect(self.reload)
        self._show_excluded = QCheckBox("Show excluded")
        self._show_excluded.toggled.connect(self.reload)
        filters.addWidget(self._filter, 1)
        filters.addWidget(self._hide_done)
        filters.addWidget(self._show_excluded)
        layout.addLayout(filters)

        counts = QHBoxLayout()
        self._sources_label = QLabel("No sources yet.")
        self._sources_label.setObjectName("meta")
        self._selection_label = QLabel("")
        self._selection_label.setObjectName("meta")
        self._add_btn = QPushButton("Add to library")
        self._add_btn.setObjectName("primary")
        self._add_btn.setIcon(icons.icon("plus", "#FFFFFF"))
        self._add_btn.clicked.connect(self.add_to_library)
        self._add_btn.setEnabled(False)
        counts.addWidget(self._sources_label)
        counts.addStretch(1)
        counts.addWidget(self._selection_label)
        counts.addWidget(self._add_btn)
        layout.addLayout(counts)

        self._thumbs = PosterThumbs(self._db)
        self._table = CheckableTable()
        header = CheckHeader(self._table)
        self._table.setHorizontalHeader(header)
        header.sectionClicked.connect(self._on_header_click)
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # The whole point of the tab: pick many, add them all at once.
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_menu)
        self._table.menuRequested.connect(self._on_menu)
        self._table.setItemDelegateForColumn(CHECK_COL, CheckDelegate(self))
        self._table.setItemDelegateForColumn(
            TITLE_COL, TitleDelegate(self._thumbs, self))
        self._table.setItemDelegateForColumn(SOURCE_COL, PillDelegate(self))
        self._table.setItemDelegateForColumn(STATUS_COL, PillDelegate(self))
        self._table.setItemDelegateForColumn(NOTES_COL, DashDelegate(self))
        self._table.setItemDelegateForColumn(MENU_COL, IconDelegate("dots"))
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(TITLE_COL, QHeaderView.Stretch)
        for col, width in ((CHECK_COL, 40), (MENU_COL, 44)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            header.resizeSection(col, width)
        for col, width in ((2, 70), (3, 80), (4, 84), (5, 110),
                           (SOURCE_COL, 130), (STATUS_COL, 150),
                           (NOTES_COL, 120)):
            header.resizeSection(col, width)
        self._table.sortByColumn(TITLE_COL, Qt.AscendingOrder)
        self._table.selectionModel().selectionChanged.connect(self._on_select)
        self._table.doubleClicked.connect(self._show_details)
        layout.addWidget(self._table, 1)
        return box

    def _build_queue(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        heading = QLabel("Preparing")
        heading.setObjectName("section")
        layout.addWidget(heading)

        self._queue_view = QTableView()
        self._queue_view.setModel(self._queue)
        self._queue_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._queue_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._queue_view.setShowGrid(False)
        self._queue_view.verticalHeader().setVisible(False)
        self._queue_view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self._queue_view.setItemDelegateForColumn(
            1, TitleDelegate(self._thumbs, self))
        self._queue_view.setItemDelegateForColumn(2, StateDelegate(self))
        self._queue_view.setItemDelegateForColumn(4, DashDelegate(self))
        self._queue_view.setItemDelegateForColumn(
            REMOVE_COL, IconDelegate("close", "#C9D2E6"))
        self._queue_view.clicked.connect(self._on_queue_click)
        self._queue_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._queue_view.customContextMenuRequested.connect(self._on_queue_menu)
        queue_header = self._queue_view.horizontalHeader()
        queue_header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        queue_header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, width in ((0, 56), (2, 170), (3, 80), (4, 180),
                           (REMOVE_COL, 52)):
            queue_header.resizeSection(col, width)
        queue_header.setSectionResizeMode(REMOVE_COL, QHeaderView.Fixed)
        layout.addWidget(self._queue_view, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        for label, icon, slot in (
                ("Move up", "up", lambda: self._move(-1)),
                ("Move down", "down", lambda: self._move(1)),
                ("Remove", "trash", self._remove_job),
                ("Retry failed", "refresh", self._retry_failed),
                ("Clear finished", "check", self._clear_finished)):
            button = QPushButton(label)
            button.setIcon(icons.icon(icon, "#C9D2E6"))
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        self._stop_btn = QPushButton("Stop after this film")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setIcon(icons.icon("stop", theme.DANGER))
        self._stop_btn.clicked.connect(self._stop)
        self._stop_btn.setEnabled(False)
        self._cancel_btn = QPushButton("Cancel this film")
        self._cancel_btn.setIcon(icons.icon("close", "#C9D2E6"))
        self._cancel_btn.clicked.connect(self._cancel)
        self._cancel_btn.setEnabled(False)
        row.addWidget(self._stop_btn)
        row.addWidget(self._cancel_btn)
        layout.addLayout(row)

        self._current = QLabel("Nothing is being prepared.")
        self._progress = QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setFormat("%p%")
        layout.addWidget(self._current)
        layout.addWidget(self._progress)

        self._log = LogConsole()
        self._log.setMaximumBlockCount(5000)
        # Room for the last several lines; the tables above are what the page
        # is for, and the whole log is in the terminal the app was started
        # from anyway.
        self._log.setMinimumHeight(120)
        self._log.setMaximumHeight(130)
        layout.addWidget(self._log)
        return box

    def _on_header_click(self, section):
        """The header's checkbox: select every row shown, or none."""
        if section != CHECK_COL:
            return
        header = self._table.horizontalHeader()
        if header.all_checked:
            self._table.clearSelection()
        else:
            self._table.selectAll()

    def _on_queue_menu(self, point):
        """What can be done to the job under the pointer, and only that: the
        order for one that is waiting, retry for one that failed, pause and
        stop for the one being prepared."""
        index = self._queue_view.indexAt(point)
        job = self._queue.row_at(index.row()) if index.isValid() else None
        if job is None:
            return
        self._queue_view.selectRow(index.row())
        menu = QMenu(self)
        state = job["state"]
        actions = {}
        if state == queue.WAITING:
            waiting = [r for r in self._queue.rows()
                       if r["state"] == queue.WAITING]
            first = waiting and waiting[0]["id"] == job["id"]
            last = waiting and waiting[-1]["id"] == job["id"]
            up = menu.addAction(icons.icon("up", "#C9D2E6"), "Move up")
            up.setEnabled(not first)
            down = menu.addAction(icons.icon("down", "#C9D2E6"), "Move down")
            down.setEnabled(not last)
            menu.addSeparator()
            actions[up] = lambda: self._move(-1)
            actions[down] = lambda: self._move(1)
            actions[menu.addAction(icons.icon("trash", "#C9D2E6"),
                                   "Remove from the queue")] = self._remove_job
        elif state == queue.FAILED:
            actions[menu.addAction(icons.icon("refresh", "#C9D2E6"),
                                   "Retry")] = lambda: self._retry_one(job)
            menu.addSeparator()
            actions[menu.addAction(icons.icon("trash", "#C9D2E6"),
                                   "Remove")] = self._remove_job
        elif state == queue.RUNNING and self._worker is not None:
            if self._queue.paused:
                actions[menu.addAction(icons.icon("play", "#C9D2E6"),
                                       "Resume")] = self._resume
            else:
                actions[menu.addAction(icons.icon("pause", "#C9D2E6"),
                                       "Pause")] = self._pause
            menu.addSeparator()
            actions[menu.addAction(icons.icon("close", theme.DANGER),
                                   "Stop this film")] = self._cancel
            actions[menu.addAction(icons.icon("stop", "#C9D2E6"),
                                   "Stop the queue after this film")] = self._stop
        if not actions:
            return
        chosen = menu.exec(self._queue_view.viewport().mapToGlobal(point))
        if chosen in actions:
            actions[chosen]()

    def _on_queue_click(self, index):
        if index.column() != REMOVE_COL:
            return
        self._queue_view.selectRow(index.row())
        self._remove_job()

    # --- loading -----------------------------------------------------------

    def reload(self):
        """Redraw from the database. Cheap, and the only way anything here
        changes: every button writes a row and calls this."""
        # Never `missing`: a file a rescan no longer finds is not something
        # anybody can choose. The row stays in the database -- a prepared
        # film's row is how it is later taken off the share, and a file that
        # comes back is revived by the next scan -- it just is not offered.
        states = [catalog.AVAILABLE, catalog.QUEUED, catalog.PREPARING,
                  catalog.READY, catalog.FAILED]
        if self._show_excluded.isChecked():
            states.append(catalog.EXCLUDED)
        rows = catalog.items(self._db, states=states)
        if self._hide_done.isChecked():
            rows = [r for r in rows if not r["in_library"]]
        self._thumbs.clear()
        self._media.set_rows(rows)
        self._queue.set_rows(list(queue.pending(self._db))
                             + list(queue.unresolved_failures(self._db)))
        self._draw_sources()
        self._on_select()
        self.queueChanged.emit()

    def _draw_sources(self):
        sources = catalog.sources(self._db)
        if not sources:
            self._sources_label.setText(
                "No sources yet -- add a folder of films to see them here.")
            self.summaryChanged.emit("No sources yet")
            return
        counts = catalog.counts(self._db)
        total = sum(counts.values())
        missing = [s for s in sources if not os.path.isdir(s["path"])]
        text = "%d source(s), %d film(s)" % (len(sources), total)
        self.summaryChanged.emit(text)
        if missing:
            text += " -- %d source(s) not reachable right now" % len(missing)
        self._sources_label.setText(text)

    def refresh_roots(self):
        self._watcher.set_roots(scan.roots(self._db))

    def use_share(self):
        """The share from Settings, as a source -- at startup and whenever
        Settings is saved. Scanned straight away when it is new; otherwise it
        is scanned like any other source."""
        _source, added = catalog.ensure_share_source(
            self._db, self._settings.nas_root)
        self.refresh_roots()
        if added:
            self.rescan()
        else:
            self.reload()

    # --- sources -----------------------------------------------------------

    def add_source(self):
        path = QFileDialog.getExistingDirectory(self, "Add a source folder")
        if not path:
            return
        catalog.add_source(self._db, path)
        self.refresh_roots()
        self.rescan()

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
                                      progress=say),
            label="scan")
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
        header = self._table.horizontalHeader()
        everything = self._proxy.rowCount()
        header.all_checked = bool(everything) and len(rows) == everything
        header.viewport().update()
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

    def _show_details(self, index):
        """Everything known about one film, in a window of its own.

        Not modal, so two films can be compared side by side and the queue can
        be watched while one is open."""
        row = self._media.row_at(self._proxy.mapToSource(index).row())
        if row is None:
            return
        FilmDetailsDialog(self._db, self._settings, row["id"], self).show()

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
        worker.jobProgress.connect(self._on_progress)
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
            self._worker.stop()             # resumes a paused film, too
            self._set_paused(False)
            self._current.setText("Stopping after this film...")
            self._stop_btn.setEnabled(False)

    def _cancel(self):
        if self._worker:
            self._worker.cancel_current()
            self._set_paused(False)
            self._current.setText("Cancelling this film...")

    def _pause(self):
        if self._worker:
            self._worker.pause()
            self._set_paused(True)

    def _resume(self):
        if self._worker:
            self._worker.resume()
            self._set_paused(False)

    def _set_paused(self, held):
        """The table's State column, and the line over the progress bar."""
        self._queue.paused = held
        self._queue.layoutChanged.emit()
        if held:
            self._job_line[2] = "paused"
        elif self._job_line[2] == "paused":
            self._job_line[2] = ""
        self._draw_current()

    def _move(self, delta):
        row = self._queue_view.currentIndex().row()
        job = self._queue.row_at(row)
        if job is not None and queue.move(self._db, job["id"], delta):
            self.reload()

    def _retry_one(self, job):
        queue.enqueue(self._db, job["item_id"])
        self.reload()
        self.start_queue()

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
        failed = queue.unresolved_failures(self._db)
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
        self._progress.setRange(0, 0)          # until ffmpeg says how far
        self._job_line = ["%s -- %d left in the queue"
                          % (event["title"], event["remaining"]), "", ""]
        self._draw_current()
        self._log.appendPlainText("--- preparing %s ---" % event["title"])

    def _on_progress(self, label, fraction):
        """ffmpeg's position in the step it is on -- the encode, the copy, a
        language's audio -- so the bar fills once per step, as the label
        beside it says."""
        if self._progress.maximum() != 1000:
            self._progress.setRange(0, 1000)
        self._progress.setValue(int(fraction * 1000))
        self._job_line[2] = "%s: %d%%" % (label, int(fraction * 100))
        self._draw_current()

    def _on_phase(self, phase, detail):
        # A new phase is a new step; its percentage, if it has one, follows.
        self._job_line[1] = "%s%s" % (phase, (": " + detail) if detail else "")
        self._job_line[2] = ""
        if self._progress.maximum() == 1000:
            self._progress.setRange(0, 0)
        self._draw_current()
        self._note_push(phase, detail)

    def _draw_current(self):
        """The film, how many are left, the phase, and ffmpeg's share of it."""
        self._current.setText(" -- ".join(b for b in self._job_line if b))

    def _note_push(self, phase, detail):
        """The last step of preparing a film is telling the stick about it,
        and that is a sync like any other -- but it happens on a tab somebody
        is probably not looking at, at the end of an encode they walked away
        from.

        So it is announced to the shared indicator, which means the Library tab
        shows "syncing" while the film it is about to gain goes over. The phase
        arrives as a stream of strings rather than a start and a stop, so the
        edge is found here: the first "pushing" opens it and anything else
        closes it. Nothing else in the queue's phases can follow a push, so the
        close is only really the belt to the braces in _on_job_done.
        """
        if phase == "pushing":
            if not self._pushing:
                self._pushing = True
                self._monitor.sync_begin(detail or "telling the stick")
            else:
                self._monitor.sync_say(detail)
        elif self._pushing:
            self._end_push()

    def _end_push(self, error=None, ok=False):
        if not self._pushing:
            return
        self._pushing = False
        if ok:
            self._monitor.sync_done()
        else:
            self._monitor.sync_end(error)

    def _on_job_done(self, result):
        self._end_push(error=result.get("push_error"),
                       ok=bool(result.get("pushed")))
        self._log.appendPlainText(
            "--- %s: %s onto the share in %ss%s ---"
            % (result.get("title"), fmt.human_size(result.get("bytes")),
               result.get("seconds"),
               "" if result.get("pushed") else ", waiting for the stick"))
        if result.get("pushed"):
            self.libraryChanged.emit()

    def _on_job_failed(self, event):
        # Ended without a verdict on purpose: a job that failed says nothing
        # about whether the stick is there. The failure is almost always
        # ffmpeg, and painting the indicator red for it would be a lie told in
        # the one colour people trust.
        self._end_push()
        self._current.setText("%s failed: %s" % (event["title"], event["error"]))

    def _on_queue_drained(self):
        self._end_push()
        self._set_paused(False)
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
