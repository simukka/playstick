"""The Device tab: what the stick is doing, and giving it its library.

Status is polled on a timer; every call runs on a worker thread so an
unreachable stick shows "offline" instead of freezing the window.

THIS TAB IS HOW FILMS GET ONTO THE DEVICE. It used to be a remote control with
a metadata form bolted on, because the library reached the stick by itself --
the preparer wrote an index onto the share and the daemon read it. The daemon
has its own database now and reads nothing from the share but the films
themselves, so "Sync library to device" is the step that was previously
invisible, and without it a freshly provisioned stick shows an empty grid.

There was a second metadata editor here, a combo box and six fields writing to
/api/admin/item. It is gone. The Library tab edits the device directly now --
with the plot, the TMDb search and a real per-field reset, none of which the
form here had -- so keeping it would have meant two editors for one table,
differing in what they could express and in which of them somebody remembered
to change.
"""

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..core import sync
from ..core.device_client import DeviceClient
from ..bridges import net_worker


class DeviceTab(QWidget):
    # A sync changed what the device holds, so the Library tab -- which reads
    # its films and posters from the device -- is out of date.
    libraryChanged = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._threads = set()
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
            # Not a library scan any more -- there is nothing to scan. It asks
            # the daemon to check whether the share is reachable, which is what
            # decides between a grid and "the films are unreachable".
            ("Check the share", lambda: self._client().rescan()),
        ]
        for i, (label, fn) in enumerate(buttons):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, f=fn: self._do(f))
            grid.addWidget(btn, i // 3, i % 3)
        outer.addWidget(controls)

        # --- the library, pushed ------------------------------------------
        # The device's film table comes from here and nowhere else: it has no
        # index to read and no share to walk.
        library = QGroupBox("Library")
        box = QVBoxLayout(library)
        note = QLabel(
            "The stick keeps its library in its own database. A sync sends the "
            "prepared film table and uploads only the posters whose bytes it "
            "does not already have.")
        note.setWordWrap(True)
        box.addWidget(note)
        self._library_status = QLabel("")
        self._library_status.setWordWrap(True)
        box.addWidget(self._library_status)
        row = QHBoxLayout()
        self._sync_btn = QPushButton("Sync library to device")
        self._sync_btn.clicked.connect(self._sync_library)
        pull = QPushButton("Pull edits from the stick")
        pull.setToolTip(
            "Copy the stick's metadata edits into the preparer's overrides "
            "file, so the next prep run does not overwrite them. A prep run "
            "does this on its own; this is for doing it early.")
        pull.clicked.connect(self._pull_overrides)
        row.addStretch(1)
        row.addWidget(pull)
        row.addWidget(self._sync_btn)
        box.addLayout(row)
        outer.addWidget(library)
        outer.addStretch(1)

    # --- lifecycle ---------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if self._settings.device_url:
            self._poll_status()
            self.refresh_library()
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

    # --- the library -------------------------------------------------------

    def refresh_library(self):
        """Say what the stick is holding. Public because a sync started here
        and an edit saved in the Library tab both change it."""
        if not self._settings.device_url:
            return
        self._do(lambda: self._client().admin_library(), self._on_library)

    def _on_library(self, reply):
        films = reply.get("films") or []
        edited = sum(1 for f in films if f.get("overrides"))
        postered = sum(1 for f in films if f.get("thumb_etag"))
        if not films:
            self._library_status.setText(
                "The stick has no films. Sync one over -- until then its grid "
                "is empty, and no amount of waiting fills it.")
            return
        self._library_status.setText(
            "%d film(s) on the stick, %d with a poster, %d edited."
            % (len(films), postered, edited))

    def _sync_library(self):
        """Push the prepared library. On a worker thread, because a first sync
        of a few hundred films with their posters is seconds, not milliseconds,
        and a frozen window during it looks exactly like a crash."""
        if not self._settings.device_url:
            QMessageBox.information(self, "No device", "Set the device URL first.")
            return
        output = self._settings.effective_output()
        if not output:
            QMessageBox.information(self, "No output",
                                    "Set the source library first.")
            return
        self._sync_btn.setEnabled(False)
        self._library_status.setText("Syncing...")

        def done(result):
            self._sync_btn.setEnabled(True)
            self._library_status.setText(
                "Sent %d film(s)%s. %d poster(s) uploaded, %d already there."
                % (result["written"],
                   ", removed %d" % result["removed"] if result["removed"] else "",
                   result["posters"], result["skipped"]))
            self.refresh_library()
            self.libraryChanged.emit()

        def failed(msg):
            self._sync_btn.setEnabled(True)
            self._library_status.setText("Sync failed: %s" % msg)

        job, thread = net_worker.call_with_progress(
            lambda say: sync.push(self._client(), output, reconcile=True,
                                  progress=say))
        job.done.connect(done)
        job.failed.connect(failed)
        # A first sync is a few hundred posters. Without this the button is
        # grey for half a minute with nothing else changing, which is what
        # makes somebody press it again.
        job.progress.connect(self._library_status.setText)
        self._track(thread)
        thread.start()

    def _pull_overrides(self):
        output = self._settings.effective_output()
        if not output:
            QMessageBox.information(self, "No output",
                                    "Set the source library first.")
            return
        self._do(lambda: sync.pull_overrides(self._client(), output),
                 lambda changed: self._library_status.setText(
                     "Wrote %d edit(s) into the preparer's overrides file."
                     % changed))
