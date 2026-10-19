"""Config's Sources tab: the folders the Prepare page offers films from.

The share is always one of them -- it is a source because Settings names it
(catalog.ensure_share_source) -- so it is listed but cannot be removed here;
changing the NAS root is how it changes. Every other folder was added by
somebody, and this is where it is taken away again.

Removing a source stops the scanning and nothing else. Films from it that are
already prepared stay in the library, on the share and on the stick -- the same
promise catalog.remove_source makes.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .. import icons, theme
from ..core import catalog

COLUMNS = ["Name", "Folder", "Films", ""]


class SourcesPanel(QWidget):
    # A source was added (True: scan it) or removed (False: just redraw).
    changed = Signal(bool)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self._rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        note = QLabel("The folders the Prepare page offers films from. The "
                      "share is always one of them; it changes with the NAS "
                      "root in Settings.")
        note.setObjectName("meta")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(36)
        header = self._table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.resizeSection(0, 180)
        header.resizeSection(2, 80)
        header.resizeSection(3, 260)
        self._table.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._remove = QPushButton("Remove")
        self._remove.setIcon(icons.icon("trash", "#C9D2E6"))
        self._remove.clicked.connect(self._remove_selected)
        self._remove.setEnabled(False)
        add = QPushButton("Add folder...")
        add.setObjectName("primary")
        add.setIcon(icons.icon("plus", "#FFFFFF"))
        add.clicked.connect(self._add)
        buttons.addWidget(self._remove)
        buttons.addWidget(add)
        layout.addLayout(buttons)
        self.reload()

    def reload(self):
        share = catalog.share_source_path(self._db)
        counts = catalog.source_counts(self._db)
        self._rows = list(catalog.sources(self._db))
        # The share first: it is the one that is always there.
        self._rows.sort(key=lambda s: (s["path"] != share, s["label"].lower()))
        self._table.setRowCount(len(self._rows))
        for i, source in enumerate(self._rows):
            is_share = source["path"] == share
            reachable = os.path.isdir(source["path"])
            if not reachable:
                note = ("The share -- not reachable right now" if is_share
                        else "Not reachable right now")
                colour = theme.DANGER
            else:
                note = "The share -- set in Settings" if is_share else ""
                colour = theme.MUTED
            cells = (source["label"], source["path"],
                     str(counts.get(source["id"], 0)), note)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(source["path"])
                if col == 3:
                    item.setForeground(QColor(colour))
                self._table.setItem(i, col, item)
        self._on_select()

    def _selected(self):
        rows = self._table.selectionModel().selectedRows()
        return self._rows[rows[0].row()] if rows else None

    def _on_select(self):
        source = self._selected()
        self._remove.setEnabled(
            source is not None
            and source["path"] != catalog.share_source_path(self._db))

    def _add(self):
        path = QFileDialog.getExistingDirectory(self, "Add a source folder")
        if not path:
            return
        catalog.add_source(self._db, path)
        self.reload()
        self.changed.emit(True)

    def _remove_selected(self):
        source = self._selected()
        if source is None or source["path"] == catalog.share_source_path(self._db):
            return
        answer = QMessageBox.question(
            self, "Remove this source?",
            "Stop offering films from %s?\n\nFilms from it that are already "
            "in the library stay in the library -- this only stops looking "
            "there." % source["path"])
        if answer != QMessageBox.Yes:
            return
        catalog.remove_source(self._db, source["id"])
        self.reload()
        self.changed.emit(False)
