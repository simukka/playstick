"""The Device tab: what the stick is doing, and giving it its library.

THIS TAB IS NOT HOW FILMS GET ONTO THE DEVICE ANY MORE. It was, briefly: the
preparer worked a whole library and this carried the result over in one press.
Films now reach the stick one at a time, as each preparation finishes, from the
Prepare tab -- which is where somebody is already watching.

First, whether the stick is running what this machine would deploy -- see
core/release.py -- and the button that deploys it when it is not. The deploy
itself runs in Config's console, where its output can be read.

Then recovery, and there are exactly two kinds.

"Push what is waiting" is the ordinary one. A queue that runs for an evening
will outlast anybody's patience for leaving the stick switched on, and a film
whose push failed is still prepared and still on the share. This sends those,
and costs no ffmpeg.

"Re-send everything" is for a stick that has been re-provisioned and has an
empty database. It reconciles, which means it also deletes films the stick has
that this machine does not know about -- another admin machine's work, or
anything synced before this database existed. That is the right thing after a
rebuild and the wrong thing at almost any other moment, so it asks first.

WHERE THE POLL WENT

This tab used to own a timer and ask the stick how it was every four seconds,
and keep the answer. It does not any more: the DeviceMonitor polls, and this is
one of the things listening. Whether the stick answered is not a fact about
this tab -- the Library tab needs it to explain an empty table, and a push needs
it to explain itself -- so it is shown by the same indicator in both places.

What this tab still wants from a poll is the part only a remote control cares
about: what is playing, how loud, and whether the lamp is on.

There was a second metadata editor here, a combo box and six fields writing to
/api/admin/item. It is gone. The Library tab edits the device directly now --
with the plot, the TMDb search and a real per-field reset, none of which the
form here had -- so keeping it would have meant two editors for one table,
differing in what they could express and in which of them somebody remembered
to change.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .device_status_bar import DeviceStatusBar
from ..core import prepared, release, sync
from ..core.device_client import DeviceClient
from ..bridges import net_worker


class DeviceTab(QWidget):
    # A sync changed what the device holds, so the Library tab -- which reads
    # its films and posters from the device -- is out of date.
    libraryChanged = Signal()
    # "Update device": the main window asks for the sudo password and hands
    # the deploy to the Config tab's console.
    updateRequested = Signal()

    def __init__(self, db, settings, monitor, parent=None):
        super().__init__(parent)
        self._db = db
        self._settings = settings
        self._monitor = monitor
        self._threads = set()
        self._build()
        monitor.status.connect(self._on_status)

    def _client(self):
        return DeviceClient(self._settings.device_url)

    def _build(self):
        outer = QVBoxLayout(self)

        outer.addWidget(DeviceStatusBar(self._monitor))

        software = QGroupBox("Software")
        row = QHBoxLayout(software)
        self._release_note = QLabel("Waiting for the stick to answer.")
        self._release_note.setWordWrap(True)
        self._update_btn = QPushButton("Update device")
        self._update_btn.setObjectName("primary")
        self._update_btn.setToolTip(
            "Deploy this machine's playstick to the stick: "
            "ansible-playbook site.yml, in Config's console.")
        self._update_btn.clicked.connect(self.updateRequested)
        self._update_btn.setVisible(False)
        row.addWidget(self._release_note, 1)
        row.addWidget(self._update_btn)
        outer.addWidget(software)
        self._device_release = None

        self._playing = QLabel("")
        outer.addWidget(self._playing)

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
            btn.clicked.connect(
                lambda _=False, f=fn, name=label: self._do(f, label=name.lower()))
            grid.addWidget(btn, i // 3, i % 3)
        outer.addWidget(controls)

        # --- the library, pushed ------------------------------------------
        # The device's film table comes from here and nowhere else: it has no
        # index to read and no share to walk.
        library = QGroupBox("Library")
        box = QVBoxLayout(library)
        note = QLabel(
            "Films reach the stick from the Prepare tab, one at a time, as "
            "each one finishes. What is here is for when that did not get "
            "through -- usually because the stick was switched off.")
        note.setWordWrap(True)
        box.addWidget(note)
        self._library_status = QLabel("")
        self._library_status.setWordWrap(True)
        box.addWidget(self._library_status)
        row = QHBoxLayout()
        self._push_btn = QPushButton("Push what is waiting")
        self._push_btn.setObjectName("primary")
        self._push_btn.setToolTip(
            "Send every film that is prepared here and that the stick has not "
            "been told about. No ffmpeg: these are already made.")
        self._push_btn.clicked.connect(self._push_pending)
        self._sync_btn = QPushButton("Re-send everything...")
        self._sync_btn.setToolTip(
            "For a stick that has been re-provisioned and has an empty "
            "database. Also removes films the stick has that this machine "
            "does not know about.")
        self._sync_btn.clicked.connect(self._resend_everything)
        row.addStretch(1)
        row.addWidget(self._sync_btn)
        row.addWidget(self._push_btn)
        box.addLayout(row)
        outer.addWidget(library)
        outer.addStretch(1)

    # --- lifecycle ---------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        # Faster polling while the remote control is the thing on screen: a
        # four-second-old "paused" is wrong in a way somebody's finger notices.
        self._monitor.set_active(True)
        if self._settings.device_url:
            self.refresh_library()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._monitor.set_active(False)

    # --- helpers -----------------------------------------------------------

    def _track(self, thread):
        self._threads.add(thread)
        thread.finished.connect(lambda: self._threads.discard(thread))

    def _do(self, fn, on_ok=None, label="device"):
        """One device call on a worker thread, its outcome reported to the
        indicator either way.

        Reads and remote-control presses are not syncs -- nothing is being
        written to the stick's library -- but whether they got through is the
        same evidence a sync gives, and the freshest there is.
        """
        if not self._settings.device_url:
            QMessageBox.information(self, "No device", "Set the device IP first.")
            return
        job, thread = net_worker.call(fn, label=label)
        job.done.connect(lambda _result: self._monitor.note_reached())
        if on_ok:
            job.done.connect(on_ok)
        job.failed.connect(self._monitor.note_unreachable)
        self._track(thread)
        thread.start()

    def _on_status(self, status):
        """What the remote control needs out of a poll. Whether the poll
        answered at all is the monitor's business, and the indicator's."""
        state = status.get("state", "?")
        title = status.get("title") or ""
        self._playing.setText("%s%s" % (state, (" -- " + title) if title else ""))
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
        self._device_release = status.get("release") or ""
        self.refresh_release()

    def refresh_release(self):
        """Compare what the stick last said with what a deploy would put on
        it. Public because saving Settings changes the second half."""
        if self._device_release is None:
            return              # the stick has not answered yet
        try:
            mine = release.local(self._settings.repo_root)
        except Exception as exc:              # a script that will not run
            self._release_note.setText("Could not work out this machine's "
                                       "release: %s" % exc)
            self._update_btn.setVisible(False)
            return
        state, sentence = release.compare(self._device_release, mine)
        self._release_note.setText(sentence)
        self._update_btn.setVisible(state == "update")

    # --- the library -------------------------------------------------------

    def refresh_library(self):
        """Say what the stick is holding. Public because a sync started here
        and an edit saved in the Library tab both change it."""
        if not self._settings.device_url:
            return
        self._do(lambda: self._client().admin_library(), self._on_library,
                 label="device-library")

    def _on_library(self, reply):
        films = reply.get("films") or []
        edited = sum(1 for f in films if f.get("overrides"))
        postered = sum(1 for f in films if f.get("thumb_etag"))
        waiting = len(prepared.unpushed(self._db))
        self._push_btn.setEnabled(bool(waiting))
        note = (" %d prepared here and waiting." % waiting) if waiting else ""
        if not films:
            self._library_status.setText(
                "The stick has no films. There is no walk that would find "
                "them: an empty database is an empty grid, and the fix is a "
                "button rather than patience." + note)
            return
        self._library_status.setText(
            "%d film(s) on the stick, %d with a poster, %d edited.%s"
            % (len(films), postered, edited, note))

    def _push_pending(self):
        """Send the films that are prepared and waiting.

        On a worker thread, because a queue that ran overnight against a stick
        that was off can be a hundred films with their posters -- seconds, not
        milliseconds, and a frozen window during it looks exactly like a crash.
        """
        if not self._require_device():
            return
        self._push_btn.setEnabled(False)
        self._monitor.sync_begin("sending the films that were waiting")

        def done(result):
            self._monitor.sync_done()
            self._push_btn.setEnabled(True)
            text = "Sent %d film(s)." % result["sent"]
            if result["failed"]:
                text += " %d did not go: %s" % (
                    len(result["failed"]), result["failed"][0][1])
            self._library_status.setText(text)
            self.refresh_library()
            self.libraryChanged.emit()

        def failed(msg):
            self._monitor.sync_end(msg)
            self._push_btn.setEnabled(True)
            self._library_status.setText("Could not push: %s" % msg)

        job, thread = net_worker.call_with_progress(
            lambda say: sync.push_pending(self._client(), self._db, say),
            label="push-pending")
        job.done.connect(done)
        job.failed.connect(failed)
        job.progress.connect(self._library_status.setText)
        job.progress.connect(self._monitor.sync_say)
        self._track(thread)
        thread.start()

    def _resend_everything(self):
        """The whole library in one request, reconciled.

        Asks first, because `reconcile` also deletes films the stick has that
        this machine does not know about. After a re-provision that is the
        whole point; at any other moment it is a way to lose another machine's
        work with one press and no visible sign afterwards.
        """
        if not self._require_device():
            return
        answer = QMessageBox.question(
            self, "Re-send the whole library?",
            "This sends every film this machine has prepared, and REMOVES any "
            "film the stick has that this machine does not know about -- "
            "another admin machine's work, or anything synced before this "
            "machine's library existed.\n\nIt is meant for a stick that has "
            "just been re-provisioned. Carry on?")
        if answer != QMessageBox.Yes:
            return
        self._sync_btn.setEnabled(False)
        self._library_status.setText("Re-sending...")
        self._monitor.sync_begin("re-sending the whole library")

        def done(result):
            self._monitor.sync_done()
            self._sync_btn.setEnabled(True)
            self._library_status.setText(
                "Sent %d film(s)%s. %d poster(s) uploaded, %d already there."
                % (result["written"],
                   ", removed %d" % result["removed"] if result["removed"] else "",
                   result["posters"], result["skipped"]))
            self.refresh_library()
            self.libraryChanged.emit()

        def failed(msg):
            self._monitor.sync_end(msg)
            self._sync_btn.setEnabled(True)
            self._library_status.setText("Could not re-send: %s" % msg)

        job, thread = net_worker.call_with_progress(
            lambda say: sync.push_all(self._client(), self._db, reconcile=True,
                                      progress=say), label="push-all")
        job.done.connect(done)
        job.failed.connect(failed)
        job.progress.connect(self._library_status.setText)
        job.progress.connect(self._monitor.sync_say)
        self._track(thread)
        thread.start()

    def _require_device(self):
        if self._settings.device_url:
            return True
        QMessageBox.information(self, "No device", "Set the device IP first.")
        return False
