"""The Games page: which games the stick holds, and the import that adds them.

Unlike the Library page, this table is this machine's `game` table, not a
question asked of the stick. A game reaches the share and the stick in the
same click, so there is no prepared-but-waiting state to explain. The "On the
stick" column says whether the last push of a game got through, and "Push all"
is the repair when one did not.

An import checks each file's header first (core/roms.py). A file that is not a
game the stick can play is listed in the summary with the reason, and is not
copied anywhere.
"""

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .device_status_bar import DeviceStatusBar
from ..bridges import net_worker
from ..core import games, nas_store, roms
from ..core.device_client import DeviceClient, DeviceError

LOG = logging.getLogger(__name__)

COLUMNS = ["Title", "System", "Players", "Year", "Poster", "On the stick",
           "Notes"]

ROM_FILTER = "Games (*.nes *.gb *.gbc *.sms)"
IMAGE_FILTER = "Images (*.jpg *.jpeg *.png)"


class GameEditor(QDialog):
    """Title, year, players, the blurb on the phone, and hidden."""

    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle(row["title"])
        form = QFormLayout(self)
        self.title = QLineEdit(row["title"])
        self.year = QSpinBox()
        self.year.setRange(0, 2100)
        self.year.setSpecialValueText("unknown")
        self.year.setValue(row["year"] or 0)
        self.players = QComboBox()
        self.players.addItems(["1 player", "2 players"])
        self.players.setCurrentIndex(1 if row["players"] == 2 else 0)
        self.plot = QPlainTextEdit(row["plot"] or "")
        self.plot.setFixedHeight(90)
        self.hidden = QCheckBox("Hide from the children's grid")
        self.hidden.setChecked(bool(row["hidden"]))
        form.addRow("Title", self.title)
        form.addRow("Year", self.year)
        form.addRow("Players", self.players)
        form.addRow("About", self.plot)
        form.addRow("", self.hidden)
        buttons = QDialogButtonBox(QDialogButtonBox.Save
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def fields(self):
        return {"title": self.title.text(),
                "year": self.year.value() or None,
                "players": self.players.currentIndex() + 1,
                "plot": self.plot.toPlainText().strip(),
                "hidden": self.hidden.isChecked()}


class GamesTab(QWidget):
    def __init__(self, db, settings, monitor, parent=None):
        super().__init__(parent)
        self._db = db
        self._settings = settings
        self._monitor = monitor
        self._threads = set()
        self._rows = []
        self.header_actions = self._build_actions()
        self._build()

    def _client(self):
        return DeviceClient(self._settings.device_url)

    # -- layout -------------------------------------------------------------

    def _build_actions(self):
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        add_files = QPushButton("Add games...")
        add_files.clicked.connect(self.add_files)
        add_folder = QPushButton("Add a folder...")
        add_folder.clicked.connect(self.add_folder)
        row.addWidget(add_files)
        row.addWidget(add_folder)
        return box

    def _build(self):
        outer = QVBoxLayout(self)
        outer.addWidget(DeviceStatusBar(self._monitor))
        self._count = QLabel("")
        outer.addWidget(self._count)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            len(COLUMNS) - 1, QHeaderView.Stretch)
        self._table.doubleClicked.connect(lambda _i: self.edit_selected())
        outer.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        for label, slot in (("Edit...", self.edit_selected),
                            ("Set poster...", self.set_poster),
                            ("Play on the stick", self.play_selected),
                            ("Remove", self.remove_selected)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        push = QPushButton("Push all games")
        push.setToolTip("Send every game again, and take off the stick any "
                        "game this machine no longer has. Films are left "
                        "alone.")
        push.clicked.connect(self.push_all)
        buttons.addWidget(push)
        outer.addLayout(buttons)

    # -- drawing ------------------------------------------------------------

    def reload(self):
        self._rows = list(games.all_games(self._db))
        self._table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            cells = (
                row["title"],
                roms.SYSTEM_NAMES.get(row["system"], row["system"]),
                str(row["players"]),
                str(row["year"] or ""),
                "yes" if row["poster_bytes"] else "no poster",
                ("hidden" if row["hidden"] else "yes") if row["pushed_at"]
                else "not yet",
                row["warning"] or "",
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, row["id"])
                self._table.setItem(r, c, item)
        waiting = sum(1 for r in self._rows if not r["pushed_at"])
        self._count.setText(
            "%d game(s)%s" % (len(self._rows),
                              ", %d not on the stick yet" % waiting
                              if waiting else ""))

    def _selected(self):
        rows = sorted({i.row() for i in self._table.selectedIndexes()})
        return [self._rows[r] for r in rows if r < len(self._rows)]

    # -- importing ----------------------------------------------------------

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add games", "",
                                                ROM_FILTER)
        if paths:
            found = []
            for path in paths:
                try:
                    found.append((path, roms.read(path)))
                except roms.NotARom as exc:
                    found.append((path, str(exc)))
            self._import(found)

    def add_folder(self):
        root = QFileDialog.getExistingDirectory(self, "Add a folder of games")
        if root:
            self._import(roms.scan(root))

    def _import(self, found):
        """Copy every good file onto the share, then push them in one go.

        The copies are small and local, so they run here; the push is a
        network call, so it goes to a worker.
        """
        nas_root = self._settings.nas_root
        added, refused = [], []
        for path, rom in found:
            if isinstance(rom, str):
                refused.append("%s: %s" % (os.path.basename(path), rom))
                continue
            try:
                added.append(games.import_rom(self._db, nas_root, rom))
            except nas_store.NasError as exc:
                refused.append("%s: %s" % (os.path.basename(path), exc))
        self.reload()
        if refused:
            QMessageBox.information(
                self, "Some files were not added",
                "%d game(s) added. These were not:\n\n%s"
                % (len(added), "\n".join(refused[:30])))
        if added:
            self._push(added)

    # -- the stick ----------------------------------------------------------

    def _run(self, fn, label, done=None):
        if not self._settings.device_url:
            QMessageBox.information(self, "No stick",
                                    "No device IP is set -- Config, Settings.")
            return
        job, thread = net_worker.call(fn, label=label)
        job.done.connect(lambda _r: self._monitor.note_reached())
        job.done.connect(lambda _r: self.reload())
        if done:
            job.done.connect(done)
        job.failed.connect(self._monitor.note_unreachable)
        job.failed.connect(lambda msg: QMessageBox.warning(
            self, "The stick did not take it", msg))
        self._threads.add(thread)
        thread.finished.connect(lambda: self._threads.discard(thread))
        thread.start()

    def _push(self, ids):
        def work():
            client = self._client()
            for ident in ids:
                games.push_one(client, self._db, ident)
        self._run(work, "games")

    def push_all(self):
        self._run(lambda: games.push_all(self._client(), self._db), "games")

    def play_selected(self):
        chosen = self._selected()
        if chosen:
            ident = chosen[0]["id"]
            self._run(lambda: self._client().play(ident), "play")

    def edit_selected(self):
        chosen = self._selected()
        if not chosen:
            return
        dialog = GameEditor(chosen[0], self)
        if dialog.exec():
            games.update(self._db, chosen[0]["id"], dialog.fields())
            self.reload()
            self._push([chosen[0]["id"]])

    def set_poster(self):
        chosen = self._selected()
        if not chosen:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose a poster", "",
                                              IMAGE_FILTER)
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            QMessageBox.warning(self, "Could not read it", str(exc))
            return
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        games.set_poster(self._db, chosen[0]["id"], data, mime)
        self.reload()
        self._push([chosen[0]["id"]])

    def remove_selected(self):
        chosen = self._selected()
        if not chosen:
            return
        names = ", ".join(r["title"] for r in chosen)
        if QMessageBox.question(
                self, "Remove?",
                "Take %s off the stick and the share?" % names) \
                != QMessageBox.Yes:
            return
        ids = [r["id"] for r in chosen]
        nas_root = self._settings.nas_root

        def work():
            client = self._client()
            for ident in ids:
                games.remove(client, self._db, nas_root, ident)
        self._run(work, "games")


__all__ = ["GamesTab", "DeviceError"]
