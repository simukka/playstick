"""The library tab: what the stick is actually showing, and the editor for it.

It used to be a view of the prepared index on this machine. It is a view of the
DEVICE now, because that is where the library lives -- the index here is one
step in making a film, and the row on the stick is the film as a child sees it.
The two differ in ways worth seeing: a title corrected from another machine, a
frame the device extracted for a film nothing here has a poster for, and above
all a film prepared here that no sync has carried over yet.

With no device configured -- or one that is switched off -- it falls back to the
index alone and says so in the status line. Preparing a library is most of a day
and must not require the stick to be plugged in.
"""

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSize,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMenu, QMessageBox,
                               QPushButton, QSplitter, QTableView, QVBoxLayout,
                               QWidget)

from . import format as fmt
from .metadata_editor import MetadataEditor
from ..bridges import net_worker, poster_worker
from ..core import library_model, posters
from ..core.device_client import DeviceClient, DeviceError

COLUMNS = ["Title", "Year", "Rating", "Genres", "Match", "On the stick", "Notes"]


class MoviesModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self._movies = []
        self._rows_by_id = {}
        self._icon_cache = {}
        # film id -> the fetched poster on this machine. Filled in as the
        # worker delivers, which is why it is kept apart from the movie dicts:
        # a poster arriving must repaint one row, not rebuild the model.
        self._fetched = {}

    def set_movies(self, movies):
        self.beginResetModel()
        self._movies = movies
        self._rows_by_id = {m.get("id"): i for i, m in enumerate(movies)}
        self._icon_cache.clear()
        self.endResetModel()

    def set_poster(self, film_id, path):
        """One poster has arrived. Repaints its row and nothing else."""
        self._fetched[film_id] = path
        self._icon_cache.pop(path, None)
        row = self._rows_by_id.get(film_id)
        if row is None:
            return
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [Qt.DecorationRole])

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
        # The device's poster first: it is what the children are looking at.
        # The local file is the fallback, and covers the case that matters most
        # -- a film prepared here that has not been synced, which by definition
        # has no poster on the stick to fetch.
        path = self._fetched.get(movie.get("id")) or movie.get("poster_abspath")
        if not path:
            return None
        if path not in self._icon_cache:
            pix = QPixmap(path)
            self._icon_cache[path] = None if pix.isNull() else QIcon(
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
                # What this column answers changed with the library. It used to
                # mean "has a prep run done the work", which nobody was asking:
                # the Prepare tab already lists what is waiting. The open
                # question now is whether the stick has this film at all.
                if movie.get("unsynced"):
                    return "not synced"
                if movie.get("source") != "device":
                    return "yes" if movie.get("prepared") else "no"
                if movie.get("hidden"):
                    return "hidden"
                return "yes" if movie.get("thumb_etag") else "no poster"
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
    # The window uses it to refresh the Device tab, which is now showing a
    # title somebody just changed.
    savedOverride = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._model = MoviesModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)      # match against every column
        self._threads = set()
        self._posters = None
        self._build()

    def _client(self):
        return DeviceClient(self._settings.device_url)

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
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_menu)
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

    # --- loading -----------------------------------------------------------

    def _track(self, thread):
        self._threads.add(thread)
        thread.finished.connect(lambda: self._threads.discard(thread))

    def reload(self):
        """Ask the device what it holds, then draw. Falls back to the index.

        The device call is on a worker thread: a stick that is switched off
        takes the client's whole timeout to say so, and a window frozen for
        eight seconds on the tab somebody just clicked is indistinguishable
        from a crash.
        """
        if not self._settings.device_url:
            return self._draw(None, "")
        job, thread = net_worker.call(lambda: self._client().admin_library())
        job.done.connect(lambda reply: self._draw(reply.get("films") or [], ""))
        job.failed.connect(lambda msg: self._draw(None, msg))
        self._track(thread)
        thread.start()

    def _draw(self, device_films, error):
        output = self._settings.effective_output()
        movies = library_model.merged_movies(output, device_films) if output else []
        self._model.set_movies(movies)

        if device_films is None:
            where = ("the stick did not answer (%s) -- showing what is prepared "
                     "here" % error) if error else "no device set"
            self._count.setText("%d film(s), %s" % (len(movies), where))
        else:
            waiting = sum(1 for m in movies if m.get("unsynced"))
            note = ", %d not synced" % waiting if waiting else ""
            self._count.setText("%d film(s) on the stick%s"
                                % (len(movies) - waiting, note))
            self._load_posters(device_films)

    def _load_posters(self, device_films):
        """Pull down the posters the device holds, in the background.

        Started fresh on every reload and the previous run abandoned: a second
        refresh should not queue a second copy of the same work behind the
        first. Nothing is wasted -- a fetch already in flight still lands in the
        cache, and the new run finds it there.
        """
        if self._posters is not None:
            self._posters.stop()
        wanted = [(row["id"], row["thumb_etag"]) for row in device_films
                  if row.get("thumb_etag")]
        posters.sweep({etag for _id, etag in wanted})
        if not wanted:
            self._posters = None
            return
        worker, thread = poster_worker.start(self._client(), wanted)
        worker.ready.connect(self._model.set_poster)
        self._posters = worker
        self._track(thread)
        thread.start()

    # --- selection and actions ---------------------------------------------

    def _on_select(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._editor.set_movie(None)
            return
        source_row = self._proxy.mapToSource(rows[0]).row()
        self._editor.set_movie(self._model.movie_at(source_row))

    def _on_menu(self, point):
        index = self._table.indexAt(point)
        if not index.isValid():
            return
        movie = self._model.movie_at(self._proxy.mapToSource(index).row())
        if not movie:
            return
        menu = QMenu(self)
        clear = menu.addAction("Forget the poster on the stick")
        # Only for a film the stick actually has one for. A sync deliberately
        # leaves posters alone -- so it cannot discard one somebody uploaded --
        # which means this is the only way to say "that one was wrong".
        clear.setEnabled(bool(movie.get("thumb_etag"))
                         and bool(self._settings.device_url))
        chosen = menu.exec(self._table.viewport().mapToGlobal(point))
        if chosen is clear:
            self._clear_poster(movie)

    def _clear_poster(self, movie):
        try:
            self._client().clear_thumb(movie["id"])
        except DeviceError as exc:
            QMessageBox.warning(self, "The stick did not answer", str(exc))
            return
        self.reload()
