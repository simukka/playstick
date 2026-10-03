"""Live watching of the source roots.

QFileSystemWatcher does not recurse, so every directory under every source is
added individually and the set is refreshed whenever a directory changes -- a
new film usually arrives inside a new folder, and that folder has to be watched
too. Notifications are debounced: a copy in progress fires many times a second,
and the app only wants to re-scan once it settles.

There are several roots now rather than one. That is the only change: the ceiling
on depth matters more with six of them than it did with one, and a root that is
not currently mounted is skipped rather than being an error -- an external drive
that is unplugged is an ordinary Tuesday.
"""

import logging
import os

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtCore import QFileSystemWatcher

LOG = logging.getLogger(__name__)

# A ceiling on how deep to watch, so pointing at a huge tree does not add tens of
# thousands of watches. The preparer only descends a few levels anyway.
MAX_WATCH_DEPTH = 4


class LibraryWatcher(QObject):
    changed = Signal()

    def __init__(self, debounce_ms=1500, parent=None):
        super().__init__(parent)
        self._roots = []
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self.changed)

    def set_roots(self, roots):
        self._clear()
        self._roots = [r for r in (roots or []) if r]
        for root in self._roots:
            if os.path.isdir(root):
                self._add_tree(root)
        LOG.debug("watching %d director%s under %d source(s)",
                  len(self._watcher.directories()),
                  "y" if len(self._watcher.directories()) == 1 else "ies",
                  len(self._roots))

    def _clear(self):
        dirs = self._watcher.directories()
        if dirs:
            self._watcher.removePaths(dirs)

    def _add_tree(self, root):
        paths = []
        root_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, _files in os.walk(root):
            depth = dirpath.count(os.sep) - root_depth
            if depth >= MAX_WATCH_DEPTH:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            paths.append(dirpath)
        if paths:
            self._watcher.addPaths(paths)

    def _on_dir_changed(self, path):
        LOG.debug("%s changed; a re-scan is queued", path)
        # A directory changing may mean a new sub-directory to watch. Re-add the
        # tree (addPaths ignores ones already watched) and debounce the re-scan.
        for root in self._roots:
            if os.path.isdir(root):
                self._add_tree(root)
        self._timer.start()
