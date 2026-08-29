"""Live watching of the source library.

QFileSystemWatcher does not recurse, so every directory under the library is
added individually and the set is refreshed whenever a directory changes -- a
new film usually arrives inside a new folder, and that folder has to be watched
too. Notifications are debounced: a copy in progress fires many times a second,
and the app only wants to re-scan once it settles.
"""

import os

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtCore import QFileSystemWatcher

# A ceiling on how deep to watch, so pointing at a huge tree does not add tens of
# thousands of watches. The preparer only descends a few levels anyway.
MAX_WATCH_DEPTH = 4


class LibraryWatcher(QObject):
    changed = Signal()

    def __init__(self, debounce_ms=1500, parent=None):
        super().__init__(parent)
        self._root = ""
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self.changed)

    def set_root(self, root):
        self._clear()
        self._root = root or ""
        if self._root and os.path.isdir(self._root):
            self._add_tree(self._root)

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

    def _on_dir_changed(self, _path):
        # A directory changing may mean a new sub-directory to watch. Re-add the
        # tree (addPaths ignores ones already watched) and debounce the re-scan.
        if self._root and os.path.isdir(self._root):
            self._add_tree(self._root)
        self._timer.start()
