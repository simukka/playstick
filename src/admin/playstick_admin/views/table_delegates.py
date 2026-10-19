"""How the Prepare tab's tables draw the cells that are more than text.

Every delegate here lets the style draw the cell first, with the text taken out
-- so selection, the row rule and hover come from the style sheet like every
plain cell's -- and then paints its own content on top.

They read the model through roles rather than reaching into it: the tables sit
behind a sort/filter proxy, and an index's row is not the model's row.
"""

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QFontMetrics, QIcon, QPainter, QPainterPath,
                           QPixmap)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .. import icons, theme
from ..core import prepared

ID_ROLE = Qt.UserRole + 1        # the film's id, for its poster
KIND_ROLE = Qt.UserRole + 2      # a key into theme.PILLS, or an icon name

ROW_HEIGHT = 36
THUMB = QSize(24, 34)


class PosterThumbs:
    """Small posters for the table, read out of the admin database on first
    paint and kept until the table is redrawn. A film with no poster -- most
    of what a source offers, until it is prepared -- gets the placeholder."""

    def __init__(self, db):
        self._db = db
        self._cache = {}
        self._placeholder = None

    def clear(self):
        self._cache.clear()

    def get(self, ident, ratio):
        if ident not in self._cache:
            self._cache[ident] = self._load(ident, ratio)
        return self._cache[ident]

    def _load(self, ident, ratio):
        row = prepared.poster(self._db, ident) if ident else None
        if row is None:
            return None
        pm = QPixmap()
        if not pm.loadFromData(bytes(row["bytes"])):
            return None
        pm = pm.scaled(THUMB * ratio, Qt.KeepAspectRatioByExpanding,
                       Qt.SmoothTransformation)
        pm.setDevicePixelRatio(ratio)
        return pm


def _base(delegate, painter, option, index):
    """The cell as the style draws it, without its text."""
    opt = QStyleOptionViewItem(option)
    delegate.initStyleOption(opt, index)
    opt.text = ""
    opt.icon = QIcon()
    widget = option.widget
    style = widget.style() if widget else None
    if style is not None:
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
    return opt


class _Delegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), ROW_HEIGHT)


class TitleDelegate(_Delegate):
    """A poster thumbnail and the title beside it."""

    def __init__(self, thumbs, parent=None):
        super().__init__(parent)
        self._thumbs = thumbs

    def paint(self, painter, option, index):
        _base(self, painter, option, index)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect
        box = QRectF(rect.left() + 8, rect.center().y() - THUMB.height() / 2,
                     THUMB.width(), THUMB.height())
        ratio = painter.device().devicePixelRatioF() or 1.0
        pm = self._thumbs.get(index.data(ID_ROLE), ratio)
        clip = QPainterPath()
        clip.addRoundedRect(box, 3, 3)
        if pm is not None:
            painter.setClipPath(clip)
            # Centred on the box, since the scale was "expanding".
            w = pm.width() / pm.devicePixelRatio()
            h = pm.height() / pm.devicePixelRatio()
            painter.drawPixmap(QRectF(box.center().x() - w / 2,
                                      box.center().y() - h / 2, w, h),
                               pm, QRectF(pm.rect()))
            painter.setClipping(False)
        else:
            painter.fillPath(clip, QColor(theme.RAISED))
            icon = icons.pixmap("poster", theme.DISABLED, 16)
            painter.drawPixmap(int(box.center().x() - 8),
                               int(box.center().y() - 8), icon)
        text_rect = rect.adjusted(int(box.width()) + 20, 0, -6, 0)
        painter.setPen(QColor(theme.TEXT))
        text = QFontMetrics(option.font).elidedText(
            str(index.data() or ""), Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.restore()


class PillDelegate(_Delegate):
    """The text in a rounded pill coloured by its kind, with a dot in front
    when the kind is a status."""

    DOTTED = ("ok", "busy", "warn", "bad")

    def paint(self, painter, option, index):
        _base(self, painter, option, index)
        text = str(index.data() or "")
        if not text:
            return self._dash(painter, option)
        kind = index.data(KIND_ROLE) or "muted"
        background, colour = theme.PILLS.get(kind, theme.PILLS["muted"])
        dotted = kind in self.DOTTED
        metrics = QFontMetrics(option.font)
        width = metrics.horizontalAdvance(text) + 18 + (14 if dotted else 0)
        width = min(width, option.rect.width() - 12)
        pill = QRectF(option.rect.left() + 6, option.rect.center().y() - 11,
                      width, 22)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(pill, 5, 5)
        painter.fillPath(path, QColor(background))
        left = pill.left() + 9
        if dotted:
            painter.setBrush(QColor(colour))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(pill.left() + 12 - 3.5, pill.center().y() - 3.5,
                                7, 7)
            left += 14
        painter.setPen(QColor(colour))
        painter.drawText(QRectF(left, pill.top(), pill.right() - left - 6,
                                pill.height()),
                         Qt.AlignVCenter | Qt.AlignLeft,
                         metrics.elidedText(text, Qt.ElideRight,
                                            int(pill.right() - left - 6)))
        painter.restore()

    @staticmethod
    def _dash(painter, option):
        painter.save()
        painter.setPen(QColor(theme.DISABLED))
        painter.drawText(option.rect.adjusted(10, 0, 0, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, "—")
        painter.restore()


class DashDelegate(_Delegate):
    """Plain text, or a dash where there is none -- an empty cell in a column
    of them reads as a rendering fault."""

    def paint(self, painter, option, index):
        if index.data():
            return super().paint(painter, option, index)
        _base(self, painter, option, index)
        PillDelegate._dash(painter, option)


class CheckDelegate(_Delegate):
    """A checkbox that shows whether the row is selected. The view, not this,
    turns a click on it into a selection toggle -- see CheckableTable."""

    def paint(self, painter, option, index):
        _base(self, painter, option, index)
        draw_check(painter, option.rect,
                   bool(option.state & QStyle.State_Selected))


def draw_check(painter, rect, checked):
    box = QRectF(rect.center().x() - 8, rect.center().y() - 8, 16, 16)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(box, 4, 4)
    if checked:
        painter.fillPath(path, QColor(theme.LINK))
        painter.drawPixmap(int(box.left()) + 1, int(box.top()) + 1,
                           icons.pixmap("check", "#FFFFFF", 14))
    else:
        painter.fillPath(path, QColor("#0F1526"))
        painter.setPen(QColor("#4A5878"))
        painter.drawPath(path)
    painter.restore()


class IconDelegate(_Delegate):
    """One icon, centred: the row's menu, a remove cross."""

    def __init__(self, name, colour=theme.MUTED, parent=None):
        super().__init__(parent)
        self._name = name
        self._colour = colour

    def paint(self, painter, option, index):
        _base(self, painter, option, index)
        icon = icons.pixmap(self._name, self._colour, 16)
        painter.drawPixmap(option.rect.center().x() - 8,
                           option.rect.center().y() - 8, icon)


class StateDelegate(_Delegate):
    """A queue job's state: the icon its kind names and the word beside it."""

    COLOURS = {"running": theme.BUSY, "clock": "#C9D2E6", "close": "#F87171",
               "pause": "#FBBF24"}

    def paint(self, painter, option, index):
        _base(self, painter, option, index)
        name = index.data(KIND_ROLE) or "clock"
        colour = self.COLOURS.get(name, theme.MUTED)
        icon = icons.pixmap(name, colour, 16)
        rect = option.rect
        painter.drawPixmap(rect.left() + 10, rect.center().y() - 8, icon)
        painter.save()
        painter.setPen(QColor(colour if name != "clock" else theme.TEXT))
        painter.drawText(rect.adjusted(34, 0, -4, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, str(index.data() or ""))
        painter.restore()
