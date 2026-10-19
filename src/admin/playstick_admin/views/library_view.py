"""The library tab: what the stick is holding, and the editor for it.

The editor is a window of its own, opened by double-clicking a film, so the
list has the page's whole width. One window, not one per film: a second film
double-clicked replaces the first in it, which is what the side panel it used
to be did, without the panel taking half the table's width all the time.

ONLY THE STICK, NOW

It used to fall back to a prepared index on this machine when the device was not
answering, and to list films prepared here that no sync had carried over as "not
synced". Both of those went with the index. A film reaches the admin database
and the stick in the same job now, so "prepared here but not over there" is a
job that failed, and the Prepare tab is where a failed job lives.

So this asks the device, and when the device is not there the table is empty and
says why. That is a deliberate loss of a fallback: the tab's question is what a
child would see on the grid, and only the stick can answer it. What is prepared
on this machine is the Prepare tab's business.

The admin database is still read, for the handful of facts only a preparation
knows -- the encode's dimensions, the TMDb confidence, what the verifier
complained about. None of those is on the device because none of them is
anything the device does.

WHICH IS WHY THE INDICATOR IS AT THE TOP

An empty table here has two very different causes -- the stick holds no films,
or the stick did not answer -- and they look identical. The count line has
always said which, in a sentence, in the corner. The indicator says it in the
place somebody looks first, in the same words and the same colour as the Device
tab, and it says the third thing the count line never could: that a film is
going over the wire right now.

TAKING A FILM BACK OUT

This is where a removal starts, because this is the list of what is IN. It goes
in one direction: off the stick, off the share, and back to "available" in the
Prepare tab, where it can be added again.
"""

import json

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSize,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPushButton, QTableView,
                               QVBoxLayout, QWidget)

from . import format as fmt
from .device_status_bar import DeviceStatusBar
from .metadata_editor import MetadataEditor
from ..bridges import net_worker, poster_worker
from ..core import device_library, pipeline, posters, prepared
from ..core.device_client import DeviceClient, DeviceError

COLUMNS = ["Title", "Year", "Rating", "Genres", "Match", "Poster", "Notes"]


class FilmEditorWindow(QDialog):
    """The metadata editor, in a window that does not block the app: the list
    behind it stays usable, and a save redraws it."""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.resize(620, 820)
        layout = QVBoxLayout(self)
        layout.addWidget(editor)

    def show_movie(self, editor, movie):
        editor.set_movie(movie)
        self.setWindowTitle(movie.get("title") or movie.get("id") or "Film")
        self.show()
        self.raise_()
        self.activateWindow()


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
        if role == Qt.UserRole:
            # What the proxy sorts on. Not the shown title, which carries a
            # "* " for an edited film and sorted every edit to the top; and
            # numbers as numbers, so 10 does not come before 9.
            if col == 0:
                return (movie.get("sort_title") or movie.get("title")
                        or "").lower()
            if col == 1:
                return int(movie.get("year") or 0)
            if col == 2:
                try:
                    return float(movie.get("rating") or 0)
                except (TypeError, ValueError):
                    return 0.0
            return self.data(index, Qt.DisplayRole) or ""
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
                # Every row here is on the stick -- that is what this tab is
                # now -- so the open question is what it looks like on the grid.
                if movie.get("hidden"):
                    return "hidden"
                return "yes" if movie.get("thumb_etag") else "no poster"
            if col == 6:
                notes = movie.get("notes") or []
                if isinstance(notes, str):
                    # From the admin database, where it is stored as JSON.
                    try:
                        notes = json.loads(notes)
                    except ValueError:
                        return notes
                return "; ".join(notes) if isinstance(notes, list) else str(notes)
        if role == Qt.ToolTipRole:
            return movie.get("plot") or movie.get("source_rel") or ""
        return None


class LibraryTab(QWidget):
    # Emitted when the user asks to re-prepare a single film after correcting it.
    reprepareRequested = Signal(dict)
    # An edit was saved -- here AND, when a device IP is set, on the stick.
    # The window uses it to refresh the Device tab, which is now showing a
    # title somebody just changed.
    savedOverride = Signal()

    # A film was taken out of the library, so the Prepare tab should offer it
    # again.
    removedFilm = Signal()

    def __init__(self, db, settings, monitor, parent=None):
        super().__init__(parent)
        self._db = db
        self._settings = settings
        self._monitor = monitor
        self._model = MoviesModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)      # match against every column
        self._proxy.setSortRole(Qt.UserRole)
        self._threads = set()
        self._posters = None
        self._build()

    def _client(self):
        return DeviceClient(self._settings.device_url)

    def _build(self):
        outer = QVBoxLayout(self)

        # Every row below comes off the stick, so whether the stick answered is
        # the first thing to say -- and "0 films" and "it did not answer" look
        # identical in a table.
        outer.addWidget(DeviceStatusBar(self._monitor))

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
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        # The rest to what they hold. Left at Qt's default hundred pixels each,
        # they took the width the two stretching columns need.
        for col, width in ((1, 64), (2, 72), (4, 120), (5, 76), (6, 110)):
            header.resizeSection(col, width)
        self._table.setShowGrid(False)
        # A to Z to start with. Switching sorting on sorts by Qt's default,
        # which is descending, and the list opened at Z.
        self._table.sortByColumn(0, Qt.AscendingOrder)
        self._table.doubleClicked.connect(self._edit_at)
        outer.addWidget(self._table, 1)

        self._editor = MetadataEditor(self._db, self._settings, self._monitor)
        self._editor.savedOverride.connect(self.reload)
        self._editor.savedOverride.connect(self.savedOverride)
        self._editor.reprepareRequested.connect(self.reprepareRequested)
        self._editor_window = FilmEditorWindow(self._editor, self)

    # --- loading -----------------------------------------------------------

    def _track(self, thread):
        self._threads.add(thread)
        thread.finished.connect(lambda: self._threads.discard(thread))

    def reload(self):
        """Ask the device what it holds, then draw.

        The device call is on a worker thread: a stick that is switched off
        takes the client's whole timeout to say so, and a window frozen for
        eight seconds on the tab somebody just clicked is indistinguishable
        from a crash.
        """
        if not self._settings.device_url:
            return self._draw(None, "")
        job, thread = net_worker.call(lambda: self._client().admin_library(),
                                      label="library")
        job.done.connect(lambda _reply: self._monitor.note_reached())
        job.done.connect(lambda reply: self._draw(reply.get("films") or [], ""))
        job.failed.connect(self._monitor.note_unreachable)
        job.failed.connect(lambda msg: self._draw(None, msg))
        self._track(thread)
        thread.start()

    def _draw(self, device_films, error):
        if device_films is None:
            # Empty, and honest about why. This tab answers "what would a child
            # see on the grid", and that is not a question this machine can
            # answer on the stick's behalf -- what is prepared HERE is the
            # Prepare tab's business.
            self._model.set_movies([])
            waiting = len(prepared.unpushed(self._db))
            note = (" %d film(s) here are prepared and waiting for it."
                    % waiting) if waiting else ""
            self._count.setText(
                ("The stick did not answer (%s)." % error if error
                 else "No device IP set -- Config, Settings.") + note)
            return

        local = {row["item_id"]: row for row in prepared.all_prepared(self._db)}
        movies = device_library.device_movies(device_films, local)
        self._model.set_movies(movies)
        unknown = sum(1 for m in movies if not m["in_admin_db"])
        # A film the stick has that this database does not: synced before this
        # database existed, or by another machine. Worth naming rather than
        # hiding, because it is also a film "Remove from library" can only half
        # remove -- there are no artifact paths here to delete.
        note = (", %d this machine did not prepare" % unknown) if unknown else ""
        self._count.setText("%d film(s) on the stick%s" % (len(movies), note))
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

    def _edit_at(self, index):
        movie = self._model.movie_at(self._proxy.mapToSource(index).row())
        if movie:
            self._editor_window.show_movie(self._editor, movie)

    def _on_menu(self, point):
        index = self._table.indexAt(point)
        if not index.isValid():
            return
        movie = self._model.movie_at(self._proxy.mapToSource(index).row())
        if not movie:
            return
        menu = QMenu(self)
        edit = menu.addAction("Edit details...")
        menu.addSeparator()
        clear = menu.addAction("Forget the poster on the stick")
        # Only for a film the stick actually has one for. A push deliberately
        # leaves posters alone -- so it cannot discard one somebody uploaded --
        # which means this is the only way to say "that one was wrong".
        clear.setEnabled(bool(movie.get("thumb_etag"))
                         and bool(self._settings.device_url))
        menu.addSeparator()
        remove = menu.addAction("Remove from library...")
        chosen = menu.exec(self._table.viewport().mapToGlobal(point))
        if chosen is edit:
            self._edit_at(index)
        elif chosen is clear:
            self._clear_poster(movie)
        elif chosen is remove:
            self._remove(movie)

    def _clear_poster(self, movie):
        self._monitor.sync_begin("forgetting the poster for %s"
                                 % (movie.get("title") or movie["id"]))
        try:
            self._client().clear_thumb(movie["id"])
        except DeviceError as exc:
            self._monitor.sync_end(str(exc))
            QMessageBox.warning(self, "The stick did not answer", str(exc))
            return
        self._monitor.sync_done()
        self.reload()

    def _remove(self, movie):
        """Take a film out of the library: off the stick, off the share, and
        back to `available` in the Prepare tab.

        Confirmed, and the confirmation says what will actually be deleted,
        because two different things are called "removing a film" and only one
        of them is reversible by pressing a button. The original the film was
        made from is never touched -- only what preparing it created.
        """
        title = movie.get("title") or movie["id"]
        if not movie.get("in_admin_db"):
            answer = QMessageBox.question(
                self, "Remove %s?" % title,
                "This machine did not prepare %s, so it does not know which "
                "files on the share belong to it.\n\nIt can be taken off the "
                "stick, but its encode, audio and subtitles will be left "
                "behind. Carry on?" % title)
            if answer != QMessageBox.Yes:
                return
        else:
            answer = QMessageBox.question(
                self, "Remove %s?" % title,
                "Take %s off the stick and delete what preparing it made on "
                "the share.\n\nThe original film is not touched, and it can "
                "be added again from the Prepare tab." % title)
            if answer != QMessageBox.Yes:
                return

        # A removal writes to the stick before it deletes anything here, so
        # it is traffic like any other push and shows as such.
        self._monitor.sync_begin("taking %s off the stick" % title)
        job, thread = net_worker.call(
            lambda: pipeline.remove(self._db, movie["id"], self._settings,
                                    self._client()), label="remove")
        job.done.connect(lambda _result: self._monitor.sync_done())
        job.done.connect(self._on_removed)
        job.failed.connect(self._monitor.sync_end)
        job.failed.connect(
            lambda msg: QMessageBox.warning(self, "Could not remove it", msg))
        self._track(thread)
        thread.start()

    def _on_removed(self, result):
        # The editor, if it is showing a film, may be showing this one -- and
        # a save there would put a film the stick no longer has back into
        # this machine's corrections. Closed rather than guessed at.
        self._editor_window.hide()
        self._editor.set_movie(None)
        self._count.setText("Removed -- %s freed from the share."
                            % fmt.human_size(result.get("bytes", 0)))
        self.reload()
        self.removedFilm.emit()
