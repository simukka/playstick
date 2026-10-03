"""One line saying whether the stick is there and whether we are talking to it.

The same widget on the Library tab and the Device tab, reading the same
DeviceMonitor, so the two cannot disagree. They used to: the Device tab said
"Device: idle" from its own poll while the Library tab said "the stick did not
answer" from its own failed request, and both were on screen a tab apart.

A dot and two pieces of text. The dot carries the state at a glance, the
headline says it in words for anybody who cannot use the colour, and the detail
is the part somebody needs when it is wrong -- the address, or the error, or
which film is being sent right now.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

# Chosen to read on a light and a dark palette both, which the stylesheet
# cannot ask about. Nothing depends on the colour alone -- see the headline.
COLOURS = {
    "ok": "#2e9e4f",
    "bad": "#d05050",
    "busy": "#3d84d8",
    "idle": "#8a8a8a",
}


class DeviceStatusBar(QWidget):
    def __init__(self, monitor, parent=None):
        super().__init__(parent)
        self._monitor = monitor
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._headline = QLabel("")
        self._headline.setStyleSheet("font-weight: bold;")
        self._detail = QLabel("")
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        row.addWidget(self._dot)
        row.addWidget(self._headline)
        row.addWidget(self._detail, 1)

        monitor.changed.connect(self.redraw)
        self.redraw()

    def redraw(self):
        colour, headline, detail = self._monitor.state.describe()
        self._dot.setStyleSheet("color: %s; font-size: 15px;"
                                % COLOURS.get(colour, COLOURS["idle"]))
        self._headline.setText(headline)
        # Elided by the layout rather than wrapped: this is a status line, and
        # a stick reporting a paragraph of traceback should not push the table
        # below it down the window.
        self._detail.setText(detail)
        self._detail.setToolTip(detail)
