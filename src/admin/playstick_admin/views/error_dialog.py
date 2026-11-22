"""Unhandled errors, shown in the window as well as logged.

An exception in a Qt slot does not end the app: PySide logs it (log.py sends it
to the terminal) and carries on. From the window that looks like a button that
did nothing -- "Add a folder of games" with the share unmounted chose a folder,
closed the picker, and then nothing, while the reason was on a terminal that
may not even be open when the app is started from its launcher.

So every unhandled exception also opens a dialog: what failed in one line, the
traceback behind "Show Details" for a bug report. It comes through a queued
signal because a worker thread's exception arrives on that thread, and only the
GUI thread may open a window.

One dialog at a time. A fault in a timer or a poll fires again every tick, and
a stack of identical boxes is worse than none; the ones that arrive while a
dialog is open are counted on it and are still in the log in full.
"""

import traceback

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import log as log_setup


class _ErrorBridge(QObject):
    raised = Signal(str, str, str)   # summary, where, traceback text

    def __init__(self):
        super().__init__()
        self._box = None
        self._more, self._where = 0, ""
        # Queued even from the GUI thread: the slot that failed is still on
        # the stack, and a modal box opened inside it would run a nested
        # event loop under its half-finished state.
        self.raised.connect(self._show, Qt.QueuedConnection)

    def report(self, kind, value, tb, where):
        summary = "%s: %s" % (kind.__name__, value) if str(value) \
            else kind.__name__
        detail = "".join(traceback.format_exception(kind, value, tb))
        self.raised.emit(summary, where, detail)

    def _show(self, summary, where, detail):
        if self._box is not None:
            self._more += 1
            self._box.setInformativeText(
                "%s\n\n%d more error(s) since; see the log."
                % (self._where_text(self._where), self._more))
            return
        self._more, self._where = 0, where
        box = QMessageBox(QApplication.activeWindow())
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Something went wrong")
        box.setText(summary)
        box.setInformativeText(self._where_text(where))
        box.setDetailedText(detail)
        box.setStandardButtons(QMessageBox.Ok)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.finished.connect(self._closed)
        self._box = box
        box.open()

    @staticmethod
    def _where_text(where):
        aside = "" if where in ("", "MainThread") else " (on %s)" % where
        return ("That action did not finish%s. The app is still running; the "
                "full traceback is under Show Details and in the log." % aside)

    def _closed(self, _result):
        self._box = None
        self._more = 0


_bridge = None


def install():
    """Call once, after the QApplication exists."""
    global _bridge
    if _bridge is None:
        _bridge = _ErrorBridge()
        log_setup.on_unhandled(_bridge.report)
    return _bridge
