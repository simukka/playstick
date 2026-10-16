"""Stills from several points in a film, for telling two copies of it apart.

The question this answers is "which of these two is the better encode", and
the only honest answer to it is looking. So the stills are decoded at the
file's own resolution and kept as PNG, a click shows one at 1:1 rather than
squeezed into a thumbnail, and the times are rounded (see
core.film_details.frame_times) so the same moment can be found in the other
copy's window. "Save all" writes them out under names that sort by time, for
flicking between two sets in an image viewer.

The stills are taken one at a time on a worker thread, each starting when the
last arrives, and only once the tab is first looked at: six seeks into a file
on a sleeping mount are not free, and most visits to this window are not
about picture quality.
"""

import os
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QGridLayout,
                               QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from ..bridges import net_worker
from ..core import film_details

THUMB_WIDTH = 360


def clock(seconds):
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


class FramesTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = None
        self._binary = ""
        self._name = "film"
        self._times = []
        self._frames = {}          # index -> (seconds, png bytes, QPixmap)
        self._buttons = []
        self._next = 0
        self._started = False

        layout = QVBoxLayout(self)
        self._status = QLabel("Stills are taken when this tab is opened.")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._grid = QGridLayout(inner)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        self._again = QPushButton("Take them again")
        self._again.clicked.connect(self._restart)
        self._save = QPushButton("Save all...")
        self._save.clicked.connect(self._save_all)
        buttons.addStretch(1)
        buttons.addWidget(self._again)
        buttons.addWidget(self._save)
        layout.addLayout(buttons)
        self._again.setEnabled(False)
        self._save.setEnabled(False)

    def set_film(self, path, binary, name):
        self._path, self._binary, self._name = path, binary, name

    def set_duration(self, duration):
        """Called with whatever is known; the first useful one wins. If the tab
        is already open and was only waiting for this, the grabbing starts."""
        if not self._times:
            self._times = film_details.frame_times(duration)
            if self._started and self._times:
                self._restart()

    def start(self):
        """The tab has been looked at. Once per window, unless asked again."""
        if self._started:
            return
        self._started = True
        if not self._path:
            self._status.setText("There is no file to take stills from.")
        elif not self._times:
            self._status.setText("Waiting for ffprobe to say how long it is...")
        else:
            self._restart()

    def _restart(self):
        for button in self._buttons:
            button.deleteLater()
        self._buttons, self._frames, self._next = [], {}, 0
        for n, seconds in enumerate(self._times):
            button = QToolButton()
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setFixedWidth(THUMB_WIDTH + 12)
            button.setText("%s  (waiting)" % clock(seconds))
            button.setEnabled(False)
            button.clicked.connect(lambda _=False, n=n: self._view(n))
            self._grid.addWidget(button, n // 2, n % 2)
            self._buttons.append(button)
        self._again.setEnabled(False)
        self._save.setEnabled(False)
        self._grab()

    def _grab(self):
        if self._next >= len(self._times):
            self._status.setText(
                "%d stills at the file's own resolution. Click one to see it "
                "at 1:1." % len(self._frames))
            self._again.setEnabled(True)
            self._save.setEnabled(bool(self._frames))
            return
        seconds = self._times[self._next]
        self._status.setText("Taking still %d of %d, at %s..."
                             % (self._next + 1, len(self._times),
                                clock(seconds)))
        path, binary = self._path, self._binary
        job, thread = net_worker.call(
            lambda: film_details.frame(path, seconds, binary), label="frame")
        # Methods, not lambdas, for the reason FilmDetailsDialog gives: a
        # window closed mid-grab must not be reached by the stills still due.
        job.done.connect(self._on_frame)
        job.failed.connect(self._on_failed)
        thread.start()

    def _on_frame(self, png):
        n = self._next
        seconds = self._times[n]
        pixmap = QPixmap()
        button = self._buttons[n]
        if pixmap.loadFromData(png):
            self._frames[n] = (seconds, png, pixmap)
            thumb = pixmap.scaledToWidth(THUMB_WIDTH, Qt.SmoothTransformation)
            button.setIcon(thumb)
            button.setIconSize(thumb.size())
            button.setText("%s  ·  %d×%d"
                           % (clock(seconds), pixmap.width(), pixmap.height()))
            button.setEnabled(True)
        else:
            button.setText("%s  (not a picture)" % clock(seconds))
        self._next += 1
        self._grab()

    def _on_failed(self, message):
        n = self._next
        self._buttons[n].setText("%s  (failed)" % clock(self._times[n]))
        self._buttons[n].setToolTip(message)
        self._next += 1
        if not os.path.isfile(self._path):
            # Gone, or a mount that went away: the rest would fail the same
            # way, one logged traceback each.
            for button in self._buttons[self._next:]:
                button.setText(button.text().replace("waiting", "not taken"))
            self._next = len(self._times)
        self._grab()

    def _file_name(self, seconds):
        stem = re.sub(r'[\\/:*?"<>|]+', "", self._name).strip() or "film"
        return "%s %s.png" % (stem, clock(seconds).replace(":", "-"))

    def _save_all(self):
        folder = QFileDialog.getExistingDirectory(self, "Save stills to")
        if not folder:
            return
        try:
            for seconds, png, _pixmap in self._frames.values():
                with open(os.path.join(folder, self._file_name(seconds)),
                          "wb") as handle:
                    handle.write(png)
        except OSError as exc:
            QMessageBox.warning(self, "Stills", "Could not save them: %s" % exc)
            return
        self._status.setText("Saved %d stills to %s."
                             % (len(self._frames), folder))

    def _view(self, n):
        shown = sorted(self._frames)
        if n in self._frames:
            _FrameViewer(self._name, [self._frames[i] for i in shown],
                         shown.index(n), self).show()


class _FrameViewer(QDialog):
    """One still at a time, at 1:1 unless asked to fit. Left and right step
    through them, which is quicker than closing and clicking the next."""

    def __init__(self, name, frames, index, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._name = name
        self._frames = frames
        self._index = index
        self.resize(1280, 800)

        layout = QVBoxLayout(self)
        self._scroll = QScrollArea()
        self._scroll.setAlignment(Qt.AlignCenter)
        self._picture = QLabel()
        self._scroll.setWidget(self._picture)
        layout.addWidget(self._scroll, 1)

        row = QHBoxLayout()
        back = QPushButton("← Earlier")
        back.clicked.connect(lambda: self._step(-1))
        on = QPushButton("Later →")
        on.clicked.connect(lambda: self._step(1))
        self._fit = QCheckBox("Fit to window")
        self._fit.toggled.connect(self._show)
        row.addWidget(back)
        row.addWidget(on)
        row.addStretch(1)
        row.addWidget(self._fit)
        layout.addLayout(row)
        QShortcut(QKeySequence(Qt.Key_Left), self, lambda: self._step(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, lambda: self._step(1))
        self._show()

    def _step(self, by):
        self._index = (self._index + by) % len(self._frames)
        self._show()

    def _show(self):
        seconds, _png, pixmap = self._frames[self._index]
        if self._fit.isChecked():
            pixmap = pixmap.scaled(self._scroll.viewport().size(),
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._picture.setPixmap(pixmap)
        self._picture.resize(pixmap.size())
        self.setWindowTitle("%s — %s" % (self._name, clock(seconds)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit.isChecked():
            self._show()
