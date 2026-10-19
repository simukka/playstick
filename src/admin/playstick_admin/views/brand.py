"""The mark and the wordmark at the top of the sidebar.

The mark is the repo's icon.svg, which the .deb also installs as the desktop
icon. That file puts it on a rounded dark square so it holds up as an app icon
on any background; here it sits on the sidebar, which is already dark, so the
square is taken out before rendering rather than drawn as a slightly different
black box.

The wordmark is drawn rather than rendered from logo.svg because logo.svg sets
it under the mark, and the sidebar wants it beside it. The gradient on "stick"
is the one the mark's tube runs through.
"""

import os
import re

from PySide6.QtCore import QByteArray, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QWidget

ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "assets", "icon.svg")

# From icon.svg's "tube" gradient.
STICK_GRADIENT = ("#fa5a89", "#ff7a55", "#ffa04c")


def _mark():
    """A renderer for the mark alone, or None without QtSvg or the file --
    the wordmark still draws, which is most of what the sidebar needs."""
    try:
        from PySide6.QtSvg import QSvgRenderer
        with open(ICON, encoding="utf-8") as handle:
            svg = handle.read()
    except (ImportError, OSError):
        return None
    svg = re.sub(r'<rect[^>]*fill="#050509"[^>]*/>', "", svg, count=1)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    return renderer if renderer.isValid() else None


class Brand(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._mark = _mark()
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip("playstick")

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x = 0.0
        if self._mark is not None:
            # The mark's own viewBox has a margin around the tube; drawn a
            # little larger than the row so the tube itself matches the text.
            self._mark.render(p, QRectF(-6, -2, 52, 52))
            x = 44.0
        font = QFont(self.font())
        font.setPixelSize(26)
        font.setWeight(QFont.Bold)
        baseline = 33.0
        play = QPainterPath()
        play.addText(QPointF(x, baseline), font, "play")
        stick = QPainterPath()
        stick.addText(QPointF(x + play.boundingRect().width() + 1, baseline),
                      font, "stick")
        p.fillPath(play, QColor("#F6F6FA"))
        box = stick.boundingRect()
        gradient = QLinearGradient(box.topLeft(), box.topRight())
        for i, colour in enumerate(STICK_GRADIENT):
            gradient.setColorAt(i / (len(STICK_GRADIENT) - 1), QColor(colour))
        p.fillPath(stick, gradient)
        p.end()
