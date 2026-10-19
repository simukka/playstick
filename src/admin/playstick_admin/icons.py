"""The app's line icons, drawn with QPainter rather than shipped as files.

Drawn because there are twenty of them, each a few strokes, and every one is
wanted in more than one colour -- muted in the sidebar, white on a primary
button, pink on a danger one. As files that is a directory of SVGs per colour,
or QtSvg plus a recolouring step; as code it is a colour argument.

Every icon is drawn on a 24-unit square with a 1.7-unit round stroke, the
grid the mockup's icons sit on, and rendered at twice the size it is shown at
so it stays sharp on a HiDPI screen.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

STROKE = 1.7


def _pen(colour, width=STROKE):
    pen = QPen(QColor(colour), width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _film(p, c):
    p.drawRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
    p.drawLine(QPointF(7, 4), QPointF(7, 20))
    p.drawLine(QPointF(17, 4), QPointF(17, 20))
    for y in (8, 12, 16):
        p.drawLine(QPointF(3, y), QPointF(7, y))
        p.drawLine(QPointF(17, y), QPointF(21, y))


def _library(p, c):
    p.drawRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
    p.drawLine(QPointF(3, 9), QPointF(21, 9))
    p.drawLine(QPointF(9, 9), QPointF(9, 20))
    p.drawLine(QPointF(15, 9), QPointF(15, 20))
    for x in (6, 12, 18):
        p.drawLine(QPointF(x - 1, 4), QPointF(x + 1, 9))


def _monitor(p, c):
    p.drawRoundedRect(QRectF(2.5, 4, 19, 13), 2, 2)
    p.drawLine(QPointF(12, 17), QPointF(12, 20))
    p.drawLine(QPointF(8, 20.5), QPointF(16, 20.5))


def _gear(p, c):
    path = QPainterPath()
    teeth, outer, inner = 8, 9.5, 7.2
    for i in range(teeth * 4):
        angle = math.pi * 2 * i / (teeth * 4)
        radius = outer if i % 4 in (1, 2) else inner
        point = QPointF(12 + radius * math.cos(angle),
                        12 + radius * math.sin(angle))
        if i == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    p.drawPath(path)
    p.drawEllipse(QPointF(12, 12), 3, 3)


def _search(p, c):
    p.drawEllipse(QPointF(10.5, 10.5), 6, 6)
    p.drawLine(QPointF(15, 15), QPointF(20, 20))


def _plus(p, c):
    p.drawLine(QPointF(12, 5), QPointF(12, 19))
    p.drawLine(QPointF(5, 12), QPointF(19, 12))


def _arc_arrow(p, start, span, tip_at_end=True):
    rect = QRectF(4.5, 4.5, 15, 15)
    p.drawArc(rect, int(start * 16), int(span * 16))
    angle = math.radians(start + (span if tip_at_end else 0))
    x, y = 12 + 7.5 * math.cos(angle), 12 - 7.5 * math.sin(angle)
    # The arrowhead points along the arc's direction of travel.
    direction = angle + (math.pi / 2 if span > 0 else -math.pi / 2)
    for spread in (0.5, -0.5):
        back = direction + math.pi + spread
        p.drawLine(QPointF(x, y),
                   QPointF(x + 3.5 * math.cos(back), y - 3.5 * math.sin(back)))


def _refresh(p, c):
    _arc_arrow(p, 100, 270)


def _arrow(p, up):
    top, bottom = (5, 19) if up else (19, 5)
    p.drawLine(QPointF(12, top), QPointF(12, bottom))
    sign = 1 if up else -1
    p.drawLine(QPointF(12, top), QPointF(7, top + 5 * sign))
    p.drawLine(QPointF(12, top), QPointF(17, top + 5 * sign))


def _trash(p, c):
    p.drawLine(QPointF(4, 7), QPointF(20, 7))
    p.drawLine(QPointF(9.5, 7), QPointF(10, 4))
    p.drawLine(QPointF(10, 4), QPointF(14, 4))
    p.drawLine(QPointF(14, 4), QPointF(14.5, 7))
    path = QPainterPath(QPointF(6, 7))
    path.lineTo(7, 20)
    path.lineTo(17, 20)
    path.lineTo(18, 7)
    p.drawPath(path)
    p.drawLine(QPointF(10, 11), QPointF(10, 16))
    p.drawLine(QPointF(14, 11), QPointF(14, 16))


def _check(p, c):
    path = QPainterPath(QPointF(5, 12.5))
    path.lineTo(10, 17.5)
    path.lineTo(19, 7)
    p.drawPath(path)


def _stop(p, c):
    p.setBrush(QColor(c))
    p.drawRoundedRect(QRectF(6, 6, 12, 12), 1.5, 1.5)


def _close(p, c):
    p.drawLine(QPointF(6, 6), QPointF(18, 18))
    p.drawLine(QPointF(18, 6), QPointF(6, 18))


def _clock(p, c):
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.drawLine(QPointF(12, 7), QPointF(12, 12))
    p.drawLine(QPointF(12, 12), QPointF(15.5, 14))


def _running(p, c):
    faint = QColor(c)
    faint.setAlpha(70)
    p.setPen(_pen(faint, 2))
    p.drawEllipse(QPointF(12, 12), 8, 8)
    p.setPen(_pen(c, 2))
    p.drawArc(QRectF(4, 4, 16, 16), 90 * 16, -110 * 16)


def _copy(p, c):
    p.drawRoundedRect(QRectF(8, 8, 12, 12), 2, 2)
    path = QPainterPath(QPointF(16, 8))
    path.lineTo(16, 5.5)
    path.quadTo(16, 4, 14.5, 4)
    path.lineTo(5.5, 4)
    path.quadTo(4, 4, 4, 5.5)
    path.lineTo(4, 14.5)
    path.quadTo(4, 16, 5.5, 16)
    path.lineTo(8, 16)
    p.drawPath(path)


def _pause(p, c):
    p.drawLine(QPointF(9, 6), QPointF(9, 18))
    p.drawLine(QPointF(15, 6), QPointF(15, 18))


def _play(p, c):
    path = QPainterPath(QPointF(8, 5.5))
    path.lineTo(18.5, 12)
    path.lineTo(8, 18.5)
    path.closeSubpath()
    p.drawPath(path)


def _dots(p, c):
    p.setBrush(QColor(c))
    p.setPen(Qt.NoPen)
    for x in (6, 12, 18):
        p.drawEllipse(QPointF(x, 12), 1.6, 1.6)


def _terminal(p, c):
    p.drawRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
    p.drawLine(QPointF(7, 9), QPointF(10, 12))
    p.drawLine(QPointF(10, 12), QPointF(7, 15))
    p.drawLine(QPointF(12, 15.5), QPointF(16, 15.5))


def _poster(p, c):
    p.drawRoundedRect(QRectF(5, 3, 14, 18), 1.5, 1.5)
    path = QPainterPath(QPointF(10, 9))
    path.lineTo(15, 12)
    path.lineTo(10, 15)
    path.closeSubpath()
    p.drawPath(path)


DRAW = {
    "film": _film, "library": _library, "monitor": _monitor, "gear": _gear,
    "search": _search, "plus": _plus, "refresh": _refresh,
    "up": lambda p, c: _arrow(p, True), "down": lambda p, c: _arrow(p, False),
    "trash": _trash, "check": _check, "stop": _stop, "close": _close,
    "clock": _clock, "running": _running, "copy": _copy, "pause": _pause,
    "play": _play, "dots": _dots, "terminal": _terminal, "poster": _poster,
}


def pixmap(name, colour, size=18, ratio=2.0):
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24.0, size / 24.0)
    p.setPen(_pen(colour))
    p.setBrush(Qt.NoBrush)
    DRAW[name](p, colour)
    p.end()
    return pm


def icon(name, colour, disabled="#5B6680", size=18):
    """A QIcon with a dimmed copy for a disabled button, so an icon does not
    stay bright beside text the style has greyed out."""
    out = QIcon()
    out.addPixmap(pixmap(name, colour, size), QIcon.Normal)
    out.addPixmap(pixmap(name, disabled, size), QIcon.Disabled)
    return out
