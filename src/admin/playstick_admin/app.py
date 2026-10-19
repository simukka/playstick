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

The four are pages behind a sidebar rather than tabs, each under a heading that
says what it is for -- see theme.py for the look.

WHAT THE WINDOW OWNS

The database. One connection pool, opened once, handed to every tab -- because
three tabs reading the same rows should see the same rows, and because the
queue worker's thread takes its own connection from the same pool.

And the device monitor, for the same reason: whether the stick is answering is
one fact, and three tabs working it out separately is how the app came to show
"Device: idle" on one tab and "the stick did not answer" on another, a click
apart, both current. Every call any tab makes reports its outcome to it, so a
push failing turns the indicator red on the tab somebody is actually looking at
rather than the one that made the call.
"""

import logging

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from . import icons, paths
from .bridges.device_monitor import DeviceMonitor
from .core import (device_state, identity, import_legacy, nas_store, release,
                   site_vars)
from .core.db import Database
from .settings import Settings
from .views.library_view import LibraryTab
from .views.prepare_view import PrepareTab
from .views.device_panel import DeviceTab
from .views.config_editor import ConfigTab
from .views.brand import Brand
from .views.device_status_bar import COLOURS
from .views.settings_dialog import SettingsDialog

LOG = logging.getLogger(__name__)

# Sidebar icon (see icons.py), name, and the line under the page's heading.
PAGES = (
    ("film", "Prepare", "Choose films from your sources and prepare them for the stick"),
    ("library", "Library", "What the stick is holding"),
    ("monitor", "Device", "Recovery, and a remote control for the stick"),
    ("gear", "Config", "This app's settings, the appliance's variables, and deploys"),
)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Playstick")
        self.resize(1200, 800)

        self._settings = Settings()
        self._settings.migrate()
        self._device_was_reached = False
        self._db = Database(paths.db_path())
        self._db.migrate()

        self._monitor = DeviceMonitor(self._settings, parent=self)
        # What actl hands Ansible, from these settings; and no password left
        # behind by a run that never finished.
        site_vars.clear_secrets()
        site_vars.write_site(self._settings)

        self._prepare = PrepareTab(self._db, self._settings, self._monitor)
        self._library = LibraryTab(self._db, self._settings, self._monitor)
        self._device = DeviceTab(self._db, self._settings, self._monitor)
        self._config = ConfigTab(self._settings, self._db)
        self._build_pages(
            (self._prepare, self._library, self._device, self._config))

        self._wire()
        LOG.info("share: %s", self._settings.nas_root or "not set")
        if self._settings.nas_root:
            LOG.info("staging: %s",
                     nas_store.staging_root(self._settings.nas_root))
        LOG.info("device: %s", self._settings.device_url or "not set")
        self._monitor.start()

        # Before anything is drawn: a job left running by a process that died
        # goes back on the queue, and the debris of a half-made film is cleared.
        self._prepare.recover()
        self._refresh_all()
        self._prepare.use_share()

        if not self._settings.nas_root:
            self.get_started()
        else:
            self._check_share()
            self._offer_import()

    def _build_pages(self, widgets):
        self._pages = QStackedWidget()
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(228)
        nav = QVBoxLayout(side)
        nav.setContentsMargins(18, 22, 14, 16)
        nav.setSpacing(6)
        nav.addWidget(Brand())
        nav.addSpacing(30)
        self._nav = QButtonGroup(self)
        self._nav_icons = []
        # A page's own buttons -- Prepare's sources -- sit in the header, to the
        # right of its title, and change with the page.
        self._actions = QStackedWidget()
        for index, ((glyph, name, _blurb), widget) in enumerate(
                zip(PAGES, widgets)):
            self._pages.addWidget(widget)
            self._actions.addWidget(getattr(widget, "header_actions", None)
                                    or QWidget())
            button = QPushButton("  " + name)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.setIconSize(QSize(22, 22))
            self._nav_icons.append(
                (icons.icon(glyph, "#AEBBD8", size=22),
                 icons.icon(glyph, "#FFFFFF", size=22)))
            self._nav.addButton(button, index)
            nav.addWidget(button)
        nav.addStretch(1)
        nav.addWidget(self._build_status_card())
        self._nav.idClicked.connect(self._pages.setCurrentIndex)
        self._pages.currentChanged.connect(self._on_page)

        self._heading = QLabel("")
        self._heading.setObjectName("heading")
        self._blurb = QLabel("")
        self._blurb.setObjectName("subheading")
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self._heading)
        titles.addWidget(self._blurb)
        top = QHBoxLayout()
        top.addLayout(titles, 1)
        top.addWidget(self._actions)
        rule = QFrame()
        rule.setObjectName("rule")

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(28, 22, 28, 20)
        body.setSpacing(14)
        body.addLayout(top)
        body.addWidget(rule)
        body.addWidget(self._pages, 1)

        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(side)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)
        self._prepare.summaryChanged.connect(self._status_detail.setText)
        self._monitor.changed.connect(self._draw_status)
        self._draw_status()
        self._on_page(0)

    def _build_status_card(self):
        """Whether the stick is answering, and how much there is to choose
        from -- the two things worth knowing from any page."""
        card = QFrame()
        card.setObjectName("statusCard")
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(8)
        self._status_dot = QLabel("\u25cf")
        self._status_head = QLabel("")
        self._status_head.setObjectName("statusHead")
        head.addWidget(self._status_dot)
        head.addWidget(self._status_head, 1)
        self._status_detail = QLabel("")
        self._status_detail.setObjectName("statusDetail")
        self._status_detail.setWordWrap(True)
        box.addLayout(head)
        box.addWidget(self._status_detail)
        return card

    def _draw_status(self):
        colour, headline, detail = self._monitor.state.describe()
        self._status_dot.setStyleSheet(
            "color: %s; font-size: 13px;" % COLOURS.get(colour, COLOURS["idle"]))
        self._status_head.setText(headline)
        self._status_head.setToolTip(detail)

    def _on_page(self, index):
        _glyph, name, blurb = PAGES[index]
        self._heading.setText(name)
        self._blurb.setText(blurb)
        self._actions.setCurrentIndex(index)
        self._nav.button(index).setChecked(True)
        for i, (normal, current) in enumerate(self._nav_icons):
            self._nav.button(i).setIcon(current if i == index else normal)

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
        # A stick that has just come back is a Library tab showing an empty
        # table and an explanation that is no longer true. Redrawing on the
        # edge rather than on every poll: the monitor only says "changed" when
        # the answer did.
        self._monitor.changed.connect(self._on_device_changed)
        # The Settings tab under Config, and the import beside it.
        self._config.settingsSaved.connect(self._on_settings_saved)
        self._config.importRequested.connect(self.import_legacy_library)
        self._config.sourcesChanged.connect(self._on_sources_changed)
        self._device.updateRequested.connect(self.update_device)

    def _on_device_changed(self):
        """Ask the stick for its films again the moment it starts answering.

        Only on the transition. A tab that reloaded on every poll would fetch
        the whole film table four times a minute for a window left open, and a
        tab that never reloaded would keep saying the stick was off for as long
        as somebody left it on that tab.
        """
        reached = self._monitor.state.state == device_state.ONLINE
        if reached and not self._device_was_reached:
            self._library.reload()
            self._device.refresh_library()
        self._device_was_reached = reached

    def _on_reprepare(self, movie):
        self._pages.setCurrentWidget(self._prepare)
        self._prepare.enqueue_one(movie["id"])

    def _refresh_all(self):
        LOG.debug("redrawing every tab")
        self._prepare.refresh_roots()
        self._prepare.reload()
        self._library.reload()

    def _check_share(self):
        return

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
        LOG.info("importing the library already at %s", where)
        result = import_legacy.run(
            self._db, where, self._settings.legacy_source_library or where,
            where)
        LOG.info("imported %d film(s), %d poster(s), %d correction(s)",
                 result["films"], result["posters"], result["overrides"])
        QMessageBox.information(
            self, "Imported",
            "%d film(s), %d poster(s) and %d correction(s) are now in this "
            "machine's library.\n\nNothing on the share was deleted."
            % (result["films"], result["posters"], result["overrides"]))
        self._refresh_all()

    def get_started(self):
        """The first-run window, when there is no share yet. Everything after
        that is the Settings tab under Config."""
        if SettingsDialog(self._settings, self).exec():
            self._config.refresh()
            self._on_settings_saved()

    def _on_sources_changed(self, added):
        self._prepare.refresh_roots()
        if added:
            self._prepare.rescan()
        else:
            self._prepare.reload()

    def update_device(self):
        """Deploy to the stick from the Device page's button.

        The sudo password is asked for each time and handed to that one run
        -- see site_vars.write_secrets -- rather than kept: a deploy is rare,
        and a stored password that can become root on the stick is worth more
        to somebody else than the typing it saves. Empty is fine for a login
        whose sudo asks for nothing."""
        password, ok = QInputDialog.getText(
            self, "Update device",
            "The stick's sudo password for %s (empty if it needs none):"
            % (self._settings.ssh_user or "the SSH user"),
            QLineEdit.Password)
        if not ok:
            return
        self._pages.setCurrentWidget(self._config)
        if not self._config.deploy(become_password=password):
            QMessageBox.information(
                self, "Not yet",
                "Something is already running in the Deploy console, or there "
                "is no playstick checkout to deploy from.")

    def _on_settings_saved(self):
        # The site values are part of the release, so the Device page's
        # verdict may have changed with them.
        release.forget()
        self._device.refresh_release()
        # A new address means the old verdict is about a machine nobody
        # cares about any more.
        self._monitor.refresh_url()
        self._monitor.poll()
        self._check_share()
        self._refresh_all()
        # The share may be a different one now, and it is always a source.
        self._prepare.use_share()
