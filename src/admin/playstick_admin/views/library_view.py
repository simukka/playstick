"""The library tab: the prepared index on the left, the metadata editor on the
right. Selecting a film loads it into the editor, where a wrong TMDb match can
be corrected."""

import os

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSize,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QPushButton, QSplitter,
                               QTableView, QVBoxLayout, QWidget)

from . import format as fmt
from .metadata_editor import MetadataEditor

COLUMNS = ["Title", "Year", "Rating", "Genres", "Match", "Prepared", "Notes"]


class MoviesModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._movies = []
        self._icon_cache = {}

    def set_movies(self, movies):
        self.beginResetModel()
        self._movies = movies
        self._icon_cache.clear()
        self.endResetModel()

    def movie_at(self, row):
        return self._movies[row] if 0 <= row < len(self._movies) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._movies)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def _icon(self, movie):
        path = movie.get("poster_abspath")
        if not path:
            return None
        if path not in self._icon_cache:
            pix = QPixmap(path)
            if pix.isNull():
                self._icon_cache[path] = None
            else:
                self._icon_cache[path] = QIcon(
                    pix.scaled(40, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        return self._icon_cache[path]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        movie = self._movies[index.row()]
        col = index.column()
        if role == Qt.DecorationRole and col == 0:
            return self._icon(movie)
        if role == Qt.DisplayRole:
            if col == 0:
                title = movie.get("title") or "?"
                return "* " + title if movie.get("has_override") else title
            if col == 1:
                return str(movie.get("year") or "")
            if col == 2:
                return str(movie.get("rating") or "")
            if col == 3:
                return fmt.genres_str(movie)
            if col == 4:
                return fmt.match_str(movie)
            if col == 5:
                if movie.get("hidden"):
                    return "hidden"
                return "yes" if movie.get("prepared") else "no"
            if col == 6:
                notes = movie.get("notes") or []
                return "; ".join(notes) if isinstance(notes, list) else str(notes)
        if role == Qt.ToolTipRole:
            return movie.get("plot") or movie.get("source_rel") or ""
        return None


class LibraryTab(QWidget):
    # Emitted when the user asks to re-prepare a single film after correcting it.
    reprepareRequested = Signal(dict)
    # An edit was saved -- here AND, when a device URL is set, on the stick.
    # The window uses it to refresh the Device tab's film list, which is now
    # showing a title somebody just changed.
    savedOverride = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._model = MoviesModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)      # match against every column
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)

        top = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter by title, genre, year, notes...")
        self._filter.textChanged.connect(self._proxy.setFilterFixedString)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        self._count = QLabel("")
        top.addWidget(self._filter, 1)
        top.addWidget(self._count)
        top.addWidget(refresh)
        outer.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(62)
        self._table.verticalHeader().setVisible(False)
        self._table.setIconSize(QSize(40, 60))
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self._table.selectionModel().selectionChanged.connect(self._on_select)
        splitter.addWidget(self._table)

        self._editor = MetadataEditor(self._settings)
        self._editor.savedOverride.connect(self.reload)
        self._editor.savedOverride.connect(self.savedOverride)
        self._editor.reprepareRequested.connect(self.reprepareRequested)
        splitter.addWidget(self._editor)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

    def reload(self):
        from ..core import library_model
        output = self._settings.effective_output()
        movies = library_model.merged_movies(output) if output else []
        self._model.set_movies(movies)
        self._count.setText("%d film(s)" % len(movies))

    def _on_select(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._editor.set_movie(None)
            return
        source_row = self._proxy.mapToSource(rows[0]).row()
        self._editor.set_movie(self._model.movie_at(source_row))
