"""Native-semantics controls for settings and multi-select menus."""
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QScrollArea, QSizePolicy, QStyle, QStyleOptionButton,
)


class PillSwitch(QCheckBox):
    """A touch-sized switch with QCheckBox signals and accessibility semantics."""

    TRACK_WIDTH = 42
    TRACK_HEIGHT = 26

    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self):
        width = self.TRACK_WIDTH + 8
        if self.text():
            width += 10 + self.fontMetrics().horizontalAdvance(self.text())
        return QSize(width, max(36, self.fontMetrics().height() + 10))

    def minimumSizeHint(self):
        return self.sizeHint()

    def hitButton(self, position):
        # Both the pill and its label use the whole 36px-high target.
        return self.rect().contains(position)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        left = self.width() - self.TRACK_WIDTH - 4 if rtl else 4
        track = QRectF(left, (self.height() - self.TRACK_HEIGHT) / 2,
                       self.TRACK_WIDTH, self.TRACK_HEIGHT)
        color = QColor('#3b82f6' if self.isChecked() else '#58616d')
        if self.isDown():
            color = color.darker(112)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(track, self.TRACK_HEIGHT / 2, self.TRACK_HEIGHT / 2)
        diameter = self.TRACK_HEIGHT - 4
        thumb_on_right = self.isChecked() != rtl
        thumb_left = track.right() - diameter - 2 if thumb_on_right else track.left() + 2
        painter.setBrush(QColor('#ffffff'))
        painter.drawEllipse(QRectF(thumb_left, track.top() + 2, diameter, diameter))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#9ac5ff'), 2))
            painter.drawRoundedRect(track.adjusted(-3, -3, 3, 3), 16, 16)
        text = QRectF(4, 0, max(0, track.left() - 14), self.height()) if rtl else QRectF(
            track.right() + 10, 0, max(0, self.width() - track.right() - 14), self.height())
        alignment = Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft
        self.style().drawItemText(painter, text.toRect(),
                                  alignment | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextShowMnemonic,
                                  self.palette(), self.isEnabled(), self.text(), QPalette.ColorRole.WindowText)


class TickCheckBox(QCheckBox):
    """Keep a visible check mark even when a theme supplies a flat indicator."""

    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        pen = QPen(QColor('#ffffff'), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        path = QPainterPath()
        path.moveTo(indicator.left() + indicator.width() * .23, indicator.top() + indicator.height() * .51)
        path.lineTo(indicator.left() + indicator.width() * .43, indicator.top() + indicator.height() * .71)
        path.lineTo(indicator.left() + indicator.width() * .79, indicator.top() + indicator.height() * .30)
        painter.drawPath(path)


class SettingsScrollArea(QScrollArea):
    """Keep pixel-only touchpad scrolling working alongside native wheel steps."""

    def wheelEvent(self, event):
        pixels = event.pixelDelta()
        if event.angleDelta().isNull() and not pixels.isNull():
            # Qt's scrollbar wheel handler ignores pixel-only events. Deltas
            # already contain the system's natural-scrolling direction.
            vertical = abs(pixels.y()) >= abs(pixels.x())
            bar = self.verticalScrollBar() if vertical else self.horizontalScrollBar()
            delta = pixels.y() if vertical else pixels.x()
            bar.setValue(bar.value() - delta)
            event.accept()
            return
        super().wheelEvent(event)


class SettingsWheelGuard(QObject):
    """Route closed dropdown/spin wheels to the page exactly once."""

    def __init__(self, scroll_area):
        super().__init__(scroll_area)
        self.scroll_area = scroll_area
        self._controls = {}

    def protect(self, *controls):
        for control in controls:
            control.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self._controls[control] = control
            control.installEventFilter(self)
            if isinstance(control, (QAbstractSpinBox, QComboBox)):
                editor = control.lineEdit()
                if editor is not None:
                    self._controls[editor] = control
                    editor.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        control = self._controls.get(watched)
        if control is None or (isinstance(control, QComboBox) and control.view().isVisible()):
            return False
        viewport = self.scroll_area.viewport()
        forwarded = QWheelEvent(
            QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())), event.globalPosition(),
            event.pixelDelta(), event.angleDelta(), event.buttons(), event.modifiers(),
            event.phase(), event.inverted(), event.source(), event.pointingDevice())
        QApplication.sendEvent(viewport, forwarded)
        # Consuming the original prevents a second parent propagation after
        # the explicit dispatch, including wheel events on a spin's line edit.
        event.accept()
        return True
