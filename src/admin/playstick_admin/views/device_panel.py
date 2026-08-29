"""The Device tab: control the running stick, and give it its library.

Status is polled on a timer; every call runs on a worker thread so an
unreachable stick shows "offline" instead of freezing the window.

THIS TAB IS NOW HOW FILMS GET ONTO THE DEVICE. It used to be a remote control
with a metadata form bolted on, because the library reached the stick by
itself -- the preparer wrote an index onto the share and the daemon read it.
The daemon has its own database now and reads nothing from the share but the
films themselves, so "Sync library to device" is the step that was previously
invisible, and without it a freshly provisioned stick shows an empty grid.

The metadata form writes through /api/admin/item, which the daemon restricts to
a handful of display fields -- title, sort_title, year, rating, genres, hidden
-- and this respects that by sending only those.
"""

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from ..core import sync
from ..core.device_client import DeviceClient
from ..bridges import net_worker


class DeviceTab(QWidget):
    # A sync changed what the device holds, so the Library tab's poster column
    # -- which reads its thumbnails from the device -- is out of date.
    libraryChanged = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._threads = set()
        self._items = []            # last library listing, for the overlay picker
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(4000)
        self._timer.timeout.connect(self._poll_status)

    def _client(self):
        return DeviceClient(self._settings.device_url)

    def _build(self):
        outer = QVBoxLayout(self)

        self._status = QLabel("Device: unknown")
        self._status.setStyleSheet("font-weight: bold;")
        outer.addWidget(self._status)

        self._detail = QLabel("")
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self._detail)

        controls = QGroupBox("Playback")
        grid = QGridLayout(controls)
        buttons = [
            ("Pause", lambda: self._client().pause()),
            ("Resume", lambda: self._client().resume()),
            ("Stop", lambda: self._client().stop()),
            ("Vol -", lambda: self._client().volume(-5)),
            ("Vol +", lambda: self._client().volume(5)),
            ("Rescan library", lambda: self._client().rescan()),
        ]
        for i, (label, fn) in enumerate(buttons):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, f=fn: self._do(f))
            grid.addWidget(btn, i // 3, i % 3)
        outer.addWidget(controls)

        # --- the library, pushed ------------------------------------------
        # The device's film table comes from here and nowhere else: it has no
        # index to read and no share to walk. This button is what puts a
        # prepared library on it.
        sync = QGroupBox("Library")
        sync_box = QVBoxLayout(sync)
        self._sync_note = QLabel(
            "The device keeps its library in its own database. A sync sends "
            "the prepared film table and any posters it does not already have.")
        self._sync_note.setWordWrap(True)
        sync_box.addWidget(self._sync_note)
        self._sync_status = QLabel("")
        self._sync_status.setWordWrap(True)
        sync_box.addWidget(self._sync_status)
        sync_row = QHBoxLayout()
        self._sync_btn = QPushButton("Sync library to device")
        self._sync_btn.clicked.connect(self._sync_library)
        pull = QPushButton("Pull edits from device")
        pull.setToolTip(
            "Copy the device's metadata edits into the preparer's overrides "
            "file, so the next prep run does not overwrite them.")
        pull.clicked.connect(self._pull_overrides)
        sync_row.addStretch(1)
        sync_row.addWidget(pull)
        sync_row.addWidget(self._sync_btn)
        sync_box.addLayout(sync_row)
        outer.addWidget(sync)

        overlay = QGroupBox("Film metadata on the device (/api/admin/item)")
        form = QFormLayout(overlay)
        self._overlay_pick = QComboBox()
        self._overlay_pick.currentIndexChanged.connect(self._load_overlay_fields)
        self._ov_title = QLineEdit()
        self._ov_sort = QLineEdit()
        self._ov_year = QLineEdit()
        self._ov_rating = QLineEdit()
        self._ov_genres = QLineEdit()
        self._ov_hidden = QComboBox()
        self._ov_hidden.addItems(["", "true", "false"])
        form.addRow("Film", self._overlay_pick)
        form.addRow("Title", self._ov_title)
        form.addRow("Sort as", self._ov_sort)
        form.addRow("Year", self._ov_year)
        form.addRow("Rating", self._ov_rating)
        form.addRow("Genres", self._ov_genres)
        form.addRow("Hidden", self._ov_hidden)
        send_row = QHBoxLayout()
        send = QPushButton("Save on device")
        send.clicked.connect(self._send_overlay)
        reload_lib = QPushButton("Reload film list")
        reload_lib.clicked.connect(self._load_library)
        send_row.addStretch(1)
        send_row.addWidget(reload_lib)
        send_row.addWidget(send)
        form.addRow(send_row)
        outer.addWidget(overlay)
        outer.addStretch(1)

    # --- lifecycle ---------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if self._settings.device_url:
            self._poll_status()
            self._load_library()
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    # --- helpers -----------------------------------------------------------

    def _track(self, thread):
        self._threads.add(thread)
        thread.finished.connect(lambda: self._threads.discard(thread))

    def _do(self, fn, on_ok=None):
        if not self._settings.device_url:
            QMessageBox.information(self, "No device", "Set the device URL first.")
            return
        job, thread = net_worker.call(fn)
        if on_ok:
            job.done.connect(on_ok)
        job.failed.connect(lambda msg: self._status.setText("Device: offline (%s)" % msg))
        self._track(thread)
        thread.start()

    def _poll_status(self):
        self._do(lambda: self._client().status(), self._on_status)

    def _on_status(self, status):
        state = status.get("state", "?")
        title = status.get("title") or ""
        self._status.setText("Device: %s %s" % (state, ("- " + title) if title else ""))
        bits = []
        if status.get("duration"):
            bits.append("dur %ss" % int(status.get("duration") or 0))
        if status.get("volume") is not None:
            bits.append("vol %s" % status.get("volume"))
        proj = status.get("projector") or {}
        if proj.get("model"):
            bits.append("projector %s/%s" % (proj.get("model"), proj.get("power")))
        if status.get("notice"):
            bits.append("notice: %s" % status["notice"])
        self._detail.setText("   ".join(bits))

    def refresh_library(self):
        """Re-read the film list from the device. Public because an edit saved
        in the Library tab also lands on the stick, which makes this tab's list
        stale without anything here having done it."""
        if self._settings.device_url:
            self._load_library()

    def _load_library(self):
        self._do(lambda: self._client().library(), self._on_library)

    def _on_library(self, lib):
        self._items = lib.get("items", []) if isinstance(lib, dict) else []
        self._overlay_pick.blockSignals(True)
        self._overlay_pick.clear()
        for item in self._items:
            self._overlay_pick.addItem(item.get("title") or item.get("id"),
                                       item.get("id"))
        self._overlay_pick.blockSignals(False)
        self._load_overlay_fields()

    def _current_item(self):
        idx = self._overlay_pick.currentIndex()
        if 0 <= idx < len(self._items):
            return self._items[idx]
        return None

    def _load_overlay_fields(self):
        item = self._current_item()
        if not item:
            return
        self._ov_title.setText(str(item.get("title") or ""))
        self._ov_sort.setText(str(item.get("sort_title") or ""))
        self._ov_year.setText(str(item.get("year") or ""))
        self._ov_rating.setText(str(item.get("rating") or ""))
        genres = item.get("genres") or []
        self._ov_genres.setText(", ".join(genres) if isinstance(genres, list) else str(genres))
        self._ov_hidden.setCurrentText("true" if item.get("hidden") else "")

    def _send_overlay(self):
        item = self._current_item()
        if not item:
            return
        fields = {}
        if self._ov_title.text().strip():
            fields["title"] = self._ov_title.text().strip()
        if self._ov_sort.text().strip():
            fields["sort_title"] = self._ov_sort.text().strip()
        year = self._ov_year.text().strip()
        if year.isdigit():
            fields["year"] = int(year)
        rating = self._ov_rating.text().strip()
        try:
            if rating:
                fields["rating"] = round(float(rating), 1)
        except ValueError:
            pass
        genres = [g.strip() for g in self._ov_genres.text().split(",") if g.strip()]
        if genres:
            fields["genres"] = genres
        if self._ov_hidden.currentText() in ("true", "false"):
            fields["hidden"] = self._ov_hidden.currentText() == "true"
        film_id = item.get("id")
        self._do(lambda: self._client().admin_item(film_id, fields),
                 lambda _r: QMessageBox.information(self, "Saved",
                                                    "The device has the edit."))

    # --- the library -------------------------------------------------------

    def _sync_library(self):
        """Push the prepared library. On a worker thread, because a first sync
        of a few hundred films with their posters is seconds, not milliseconds,
        and a frozen window during it looks exactly like a crash."""
        output = self._settings.effective_output()
        if not output:
            QMessageBox.information(self, "No output",
                                    "Set the source library first.")
            return
        self._sync_btn.setEnabled(False)
        self._sync_status.setText("Syncing...")

        def done(result):
            self._sync_btn.setEnabled(True)
            self._sync_status.setText(
                "Sent %d film(s)%s. %d poster(s) uploaded, %d already there."
                % (result["written"],
                   ", removed %d" % result["removed"] if result["removed"] else "",
                   result["posters"], result["skipped"]))
            self._load_library()
            self.libraryChanged.emit()

        def failed(msg):
            self._sync_btn.setEnabled(True)
            self._sync_status.setText("Sync failed: %s" % msg)

        if not self._settings.device_url:
            self._sync_btn.setEnabled(True)
            self._sync_status.setText("")
            QMessageBox.information(self, "No device", "Set the device URL first.")
            return
        job, thread = net_worker.call(
            lambda: sync.push(self._client(), output, reconcile=True))
        job.done.connect(done)
        job.failed.connect(failed)
        self._track(thread)
        thread.start()

    def _pull_overrides(self):
        output = self._settings.effective_output()
        if not output:
            QMessageBox.information(self, "No output",
                                    "Set the source library first.")
            return
        self._do(lambda: sync.pull_overrides(self._client(), output),
                 lambda changed: self._sync_status.setText(
                     "Wrote %d edit(s) into the preparer's overrides file."
                     % changed))
