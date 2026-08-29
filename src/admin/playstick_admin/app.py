"""The main window: four tabs, a menu, and the wiring between them.

The tabs are steps in an order, and the order is the thing to know: Prepare
makes films on this machine, Library shows what the stick is holding and edits
it, Device carries the first onto the second. Nothing reaches the stick without
that third step -- the daemon has its own database and reads no index -- which
is why a sync finishing refreshes the Library tab and an edit saved there
refreshes the Device tab.
"""

from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from .settings import Settings
from .views.library_view import LibraryTab
from .views.prep_queue_view import PrepTab
from .views.device_panel import DeviceTab
from .views.config_editor import ConfigTab
from .views.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Playstick Admin")
        self.resize(1100, 720)
        self._settings = Settings()

        self._tabs = QTabWidget()
        self._library = LibraryTab(self._settings)
        self._prep = PrepTab(self._settings)
        self._device = DeviceTab(self._settings)
        self._config = ConfigTab(self._settings)
        self._tabs.addTab(self._prep, "Prepare")
        self._tabs.addTab(self._library, "Library")
        self._tabs.addTab(self._device, "Device")
        self._tabs.addTab(self._config, "Config")
        self.setCentralWidget(self._tabs)

        self._build_menu()
        self._wire()
        self._refresh_all()

        if not self._settings.source_library:
            self.open_settings()

    def _build_menu(self):
        menu = self.menuBar().addMenu("&File")
        settings_action = menu.addAction("Settings...")
        settings_action.triggered.connect(self.open_settings)
        refresh_action = menu.addAction("Refresh")
        refresh_action.triggered.connect(self._refresh_all)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.close)

    def _wire(self):
        # A finished run refreshes the library shelf.
        self._prep.libraryChanged.connect(self._library.reload)
        # ...and so does a sync, which is the step between a finished run and
        # the stick actually knowing about it.
        self._device.libraryChanged.connect(self._library.reload)
        # An edit saved in the Library tab is also sent to the device, so the
        # Device tab's summary of what the stick holds is stale afterwards.
        self._library.savedOverride.connect(self._device.refresh_library)
        # Correcting a film and asking to re-prepare hands off to the Prepare tab.
        self._library.reprepareRequested.connect(self._on_reprepare)

    def _on_reprepare(self, movie):
        self._tabs.setCurrentWidget(self._prep)
        self._prep.reprepare(movie)

    def _refresh_all(self):
        self._prep.refresh_root()
        self._library.reload()

    def open_settings(self):
        dialog = SettingsDialog(self._settings, self)
        if dialog.exec():
            self._refresh_all()
