"""Run actl (and thereby ansible) as a child process, streaming its output.

actl wraps ansible in a Docker container; it takes a single command string and
runs it against the inventory. This streams combined stdout/stderr line by line
so the config tab can show a live console.

Privilege prompts: a real deploy uses ansible's -K, which reads the become
password straight from the controlling tty. A QProcess is not a tty, so those
commands must be run in a real terminal -- the presets below cover the ones that
need no password (syntax check, ping, --check dry runs against unprivileged
tasks). Secrets are never routed through this tool.
"""

import os

from PySide6.QtCore import QObject, QProcess, Signal

from .. import paths


class ActlRunner(QObject):
    output = Signal(str)
    started = Signal(str)           # the command line that was launched
    finished = Signal(int)          # exit code

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None

    def is_running(self):
        return self._proc is not None and \
            self._proc.state() != QProcess.NotRunning

    def run(self, repo_root, command):
        """Launch `./actl '<command>'` in repo_root, or `./actl` with no
        argument when command is empty (its default syntax-check). No-op if one
        is already running.

        The repo is an argument rather than fixed at construction because it is
        a setting, and changing it should not need a restart."""
        if self.is_running():
            self.output.emit("[busy] a command is already running\n")
            return
        actl = paths.actl(repo_root)
        proc = QProcess(self)
        proc.setWorkingDirectory(repo_root)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self._drain)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        args = [command] if command else []
        display = "%s %s" % (os.path.basename(actl),
                             repr(command) if command else "(syntax check)")
        self.started.emit(display)
        self.output.emit("$ %s\n" % display)
        proc.start(actl, args)

    def stop(self):
        if self.is_running():
            self._proc.kill()

    def _drain(self):
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        if data:
            self.output.emit(data)

    def _on_error(self, err):
        self.output.emit("\n[process error] %s\n" % err)

    def _on_finished(self, code, _status):
        self._drain()
        self.finished.emit(int(code))
        self.output.emit("\n[exited %d]\n" % code)
        self._proc = None
