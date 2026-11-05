"""Paint launcher surfaces and honor the platform's motion preference."""

import math
import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsEffect,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QWidget,
)

COLORS = dict(re.findall(r"@([a-z0-9_]+)@ = (#[0-9a-f]{6})", Path(__file__).with_name("theme.qss").read_text(encoding="utf-8")))


def animations_enabled(widget):
    return QApplication.platformName() != "offscreen" and widget.style().styleHint(QStyle.StyleHint.SH_Widget_Animation_Duration) > 0


def chamfer(rect, cut):
    path = QPainterPath()
    path.moveTo(rect.left() + cut, rect.top())
    path.lineTo(rect.right(), rect.top())
    path.lineTo(rect.right(), rect.bottom() - cut)
    path.lineTo(rect.right() - cut, rect.bottom())
    path.lineTo(rect.left(), rect.bottom())
    path.lineTo(rect.left(), rect.top() + cut)
    path.closeSubpath()
    return path


def scanlines(painter, rect, path=None):
    painter.save()
    if path is not None:
        painter.setClipPath(path)
    else:
        painter.setClipRect(rect)
    color = QColor(COLORS["accent"])
    color.setAlpha(18)
    painter.setPen(QPen(color, 1))
    for y in range(int(rect.top()), int(rect.bottom()) + 1, 3):
        painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
    painter.restore()


class DoomFrame(QFrame):
    def paintEvent(self, event):
        name = self.objectName()
        if name not in {"card", "topbar"}:
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = chamfer(rect, 12) if name == "card" else QPainterPath()
        if name == "topbar":
            path.addRect(rect)
        painter.setPen(QPen(QColor(COLORS["line"]), 1))
        painter.setBrush(QColor(COLORS["panel"]))
        painter.drawPath(path)
        if name == "topbar":
            scanlines(painter, rect, path)
            color = QColor(COLORS["accent"])
            color.setAlpha(60)
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(0, self.height() - 0.5), QPointF(self.width(), self.height() - 0.5))


class DoomBackground(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0, QColor(COLORS["ink"]))
        gradient.setColorAt(1, QColor("#0b1012"))
        painter.fillRect(self.rect(), gradient)
        vignette = QRadialGradient(QPointF(0.5, 0.5), 0.5)
        vignette.setCoordinateMode(QRadialGradient.CoordinateMode.ObjectBoundingMode)
        vignette.setColorAt(0, QColor(0, 0, 0, 0))
        vignette.setColorAt(0.5, QColor(0, 0, 0, 0))
        vignette.setColorAt(1, QColor(0, 0, 0, 35))
        painter.fillRect(self.rect(), vignette)


class DoomButton(QPushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setProperty("doomButton", True)
        self._hover = 0.0
        self._hover_animation = QVariantAnimation(self)
        self._hover_animation.setDuration(120)
        self._hover_animation.valueChanged.connect(self._set_hover)

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def _hover_to(self, value):
        self._hover_animation.stop()
        if not animations_enabled(self):
            self._set_hover(value)
            return
        self._hover_animation.setStartValue(self._hover)
        self._hover_animation.setEndValue(value)
        self._hover_animation.start()

    def enterEvent(self, event):
        self._hover_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_to(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event):
        name = self.objectName()
        if name in {"link", "symptom", "optionCategory", "helpIcon", "stepDown", "stepUp"}:
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self.font())
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if name == "nav":
            self._paint_nav(painter, rect)
        else:
            self._paint_button(painter, rect, name)

    def _paint_button(self, painter, rect, name):
        path = chamfer(rect, min(8, rect.height() / 2))
        border = self._border_color(name)
        fill = self._fill_color(name)
        painter.setPen(QPen(border, 2 if self.hasFocus() else 1))
        painter.setBrush(fill)
        painter.drawPath(path)
        if name == "primary" or self._hover:
            painter.save()
            if name == "primary":
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            scanlines(painter, rect, path)
            painter.restore()
        foreground = self._foreground(name)
        painter.setPen(QColor(COLORS[foreground]))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text().replace("&&", "&"))

    def _fill_color(self, name):
        if not self.isEnabled():
            return QColor(COLORS["panel2"])
        if name != "primary":
            return QColor(0, 0, 0, 0)
        fill = QColor(COLORS["accent"])
        factor = 0.92 if self.isDown() else 1 + 0.08 * self._hover
        return QColor(*(min(255, round(value * factor)) for value in fill.getRgb()[:3]))

    def _foreground(self, name):
        if not self.isEnabled():
            return "muted"
        return {"primary": "ink", "danger": "bad"}.get(name, "text")

    def _border_color(self, name):
        if self.property("conflict"):
            return QColor(COLORS["warn"])
        if name == "danger":
            return QColor(COLORS["bad"])
        if name == "primary" or self.hasFocus():
            return QColor(COLORS["accent"])
        start, end = QColor(COLORS["line"]), QColor(COLORS["accent"])
        return QColor(*(round(a + (b - a) * self._hover) for a, b in zip(start.getRgb()[:3], end.getRgb()[:3], strict=True)))

    def _paint_nav(self, painter, rect):
        if self.isChecked():
            path = chamfer(rect, 8)
            painter.setPen(QPen(QColor(COLORS["line"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            scanlines(painter, rect, path)
            painter.save()
            painter.setClipPath(path)
            self._selection_bar(painter, rect)
            painter.restore()
        elif self._hover:
            painter.fillRect(QRectF(rect.left(), rect.bottom() - 1.5, rect.width(), 2), QColor(COLORS["line"]))
        painter.setPen(QColor(COLORS["text" if self.isChecked() or self._hover else "muted"]))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())

    @staticmethod
    def _selection_bar(painter, rect):
        for distance in range(8, 0, -1):
            color = QColor(COLORS["accent"])
            color.setAlpha(round(90 * math.exp(-(distance * distance) / (2 * (8 / 3) ** 2))))
            painter.fillRect(QRectF(rect.left(), rect.bottom() - distance - 1, rect.width(), 1), color)
        painter.fillRect(QRectF(rect.left(), rect.bottom() - 1.5, rect.width(), 2), QColor(COLORS["accent"]))


class DoomTabBar(QTabBar):
    def paintEvent(self, event):
        painter = QStylePainter(self)
        for index in range(self.count()):
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)
            if index == self.currentIndex():
                scanlines(painter, QRectF(self.tabRect(index)))
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabLabel, option)


class ItemScanlines(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus):
            scanlines(painter, QRectF(option.rect))


class PageFade(QGraphicsEffect):
    def __init__(self, previous, parent):
        super().__init__(parent)
        self.previous = previous
        self.progress = 0.0

    def advance(self, value):
        self.progress = float(value)
        self.update()

    def draw(self, painter):
        painter.save()
        painter.setOpacity(1 - self.progress)
        painter.drawPixmap(0, 0, self.previous)
        painter.setOpacity(0.6 + 0.4 * max(0, (self.progress - 0.375) / 0.625))
        if self.progress < 0.375:
            painter.translate(3, 0)
        self.drawSource(painter)
        painter.restore()


def fade_page(widget, previous):
    active = getattr(widget, "_page_animation", None)
    if active is not None:
        active.stop()
        active.deleteLater()
    effect = PageFade(previous, widget)
    widget.setGraphicsEffect(effect)
    animation = QVariantAnimation(widget)
    animation.setDuration(160)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.valueChanged.connect(effect.advance)
    animation.finished.connect(lambda: widget.setGraphicsEffect(None) if widget.graphicsEffect() is effect else None)
    widget._page_animation = animation
    animation.start()
