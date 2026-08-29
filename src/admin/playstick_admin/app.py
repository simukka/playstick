"""The main window: four tabs, a menu, and the wiring between them.

The tabs are steps in an order, and the order is the thing to know.

PREPARE is everything this machine can see -- several sources, most of them
nowhere near the share -- and the act of choosing which of it the stick should
hold. Choosing puts a film on a queue; the queue prepares one at a time, puts
the result on the share, and tells the stick.

LIBRARY is what the stick is holding, asked of the stick. Not what is prepared
here, and not a merge of the two: a film reaches the admin database and the
stick in the same job now, so "prepared but not over there" is a job that
failed, and that lives in Prepare.

DEVICE is recovery and a remote control. It used to be how films got onto the
stick; they arrive one at a time from Prepare now, and what is left here is for
when that did not get through.

CONFIG edits the Ansible variables and is unrelated to any of it.

WHAT THE WINDOW OWNS

The database. One connection pool, opened once, handed to every tab -- because
three tabs reading the same rows should see the same rows, and because the
queue worker's thread takes its own connection from the same pool.
"""

from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from . import paths
from .core import identity, import_legacy
from .core.db import Database
from .settings import Settings
from .views.library_view import LibraryTab
from .views.prepare_view import PrepareTab
from .views.device_panel import DeviceTab
from .views.config_editor import ConfigTab
from .views.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Playstick Admin")
        self.resize(1200, 800)

        self._settings = Settings()
        self._settings.migrate()
        self._db = Database(paths.db_path())
        self._db.migrate()

        self._tabs = QTabWidget()
        self._prepare = PrepareTab(self._db, self._settings)
        self._library = LibraryTab(self._db, self._settings)
        self._device = DeviceTab(self._db, self._settings)
        self._config = ConfigTab(self._settings)
        self._tabs.addTab(self._prepare, "Prepare")
        self._tabs.addTab(self._library, "Library")
        self._tabs.addTab(self._device, "Device")
        self._tabs.addTab(self._config, "Config")
        self.setCentralWidget(self._tabs)

        self._build_menu()
        self._wire()

        # Before anything is drawn: a job left running by a process that died
        # goes back on the queue, and the debris of a half-made film is cleared.
        self._prepare.recover()
        self._refresh_all()

        if not self._settings.nas_root:
            self.open_settings()
        else:
            self._check_share()
            self._offer_import()

    def _build_menu(self):
        menu = self.menuBar().addMenu("&File")
        settings_action = menu.addAction("Settings...")
        settings_action.triggered.connect(self.open_settings)
        refresh_action = menu.addAction("Refresh")
        refresh_action.triggered.connect(self._refresh_all)
        menu.addSeparator()
        import_action = menu.addAction("Import an existing prepared library...")
        import_action.triggered.connect(self.import_legacy_library)
        self._tidy_action = menu.addAction("Tidy the share...")
        self._tidy_action.triggered.connect(self.tidy_share)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.close)

    def _wire(self):
        # A film finished and reached the stick, so the Library tab is stale.
        self._prepare.libraryChanged.connect(self._library.reload)
        # ...and so is the Device tab's count of what is waiting.
        self._prepare.libraryChanged.connect(self._device.refresh_library)
        # A recovery push does the same from the other end.
        self._device.libraryChanged.connect(self._library.reload)
        # An edit saved in the Library tab is also sent to the stick, so the
        # Device tab's summary of what it holds is stale afterwards.
        self._library.savedOverride.connect(self._device.refresh_library)
        # A film taken out of the library is offerable again.
        self._library.removedFilm.connect(self._prepare.reload)
        # Correcting a film and asking to re-derive its metadata hands off to
        # the Prepare tab, which owns the queue.
        self._library.reprepareRequested.connect(self._on_reprepare)

    def _on_reprepare(self, movie):
        self._tabs.setCurrentWidget(self._prepare)
        self._prepare.enqueue_one(movie["id"])

    def _refresh_all(self):
        self._prepare.refresh_roots()
        self._prepare.reload()
        self._library.reload()

    def _check_share(self):
        """Refuse to work against a share this database was not built for.

        Every path in it is relative to one directory, and half the film ids
        are a hash of one. Pointed at a second share, nothing raises: the scan
        works, the encode works, and the push sends the stick paths that
        resolve to nothing on its own mount -- which the daemon answers by
        dropping the row and logging a line nobody is reading.
        """
        complaint = identity.guard_nas_root(self._db, self._settings.nas_root)
        if complaint:
            QMessageBox.warning(self, "That is a different share", complaint)

    # --- the old arrangement, brought forward -----------------------------

    def _offer_import(self):
        """Notice a library prepared before this database existed, and offer.

        Offered rather than done, because it writes a source and two hundred
        rows and somebody should know that happened. Offered once: after an
        import there is a meta row saying so, and the prompt stops.
        """
        if import_legacy.already_imported(self._db):
            return
        if self._db.one("SELECT COUNT(*) AS n FROM prepared")["n"]:
            return
        where = self._settings.nas_root
        summary = import_legacy.describe(where)
        if not summary:
            return
        answer = QMessageBox.question(
            self, "Import the library that is already here?",
            "%s\n\nReading it in keeps every film's id, so the posters, the "
            "corrections and the resume positions on the stick stay attached "
            "to the films they belong to.\n\nNothing on the share is deleted."
            % summary)
        if answer == QMessageBox.Yes:
            self.import_legacy_library()

    def import_legacy_library(self):
        where = self._settings.nas_root
        summary = import_legacy.describe(where)
        if not summary:
            QMessageBox.information(
                self, "Nothing to import",
                "There is no playstick-library.json at %s." % where)
            return
        result = import_legacy.run(
            self._db, where, self._settings.legacy_source_library or where,
            where)
        QMessageBox.information(
            self, "Imported",
            "%d film(s), %d poster(s) and %d correction(s) are now in this "
            "machine's library.\n\nNothing on the share was deleted -- File, "
            "Tidy the share, removes the files nothing reads any more."
            % (result["films"], result["posters"], result["overrides"]))
        self._refresh_all()

    def tidy_share(self):
        """Delete the five things the old arrangement left that the stick never
        opened. Says exactly what will go, and how much, before doing it."""
        where = self._settings.nas_root
        leavings = import_legacy.leavings(where)
        if not leavings:
            QMessageBox.information(
                self, "Nothing to tidy",
                "The share holds only films already.")
            return
        total = sum(size for _path, size in leavings)
        from .views.format import human_size
        answer = QMessageBox.question(
            self, "Tidy the share?",
            "%d file(s), %s, that the stick has never opened: the index, the "
            "overrides file, the probe and TMDb caches, and the posters -- "
            "which the stick keeps in its own database.\n\nThey are all in "
            "this machine's library now. Delete them from the share?"
            % (len(leavings), human_size(total)))
        if answer != QMessageBox.Yes:
            return
        try:
            result = import_legacy.tidy_share(self._db, where)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Not yet", str(exc))
            return
        QMessageBox.information(
            self, "Tidied",
            "Removed %d file(s) and freed %s on the share."
            % (result["removed"], human_size(result["bytes"])))

    def open_settings(self):
        dialog = SettingsDialog(self._settings, self)
        if dialog.exec():
            self._check_share()
            self._refresh_all()
