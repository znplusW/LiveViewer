"""Palette-aware speaker control with standard QPushButton interaction."""
from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QPushButton, QSizePolicy, QStyle, QStyleOptionButton


class SoundButton(QPushButton):
    """Checked means muted; the label describes the next click's action."""

    def __init__(self, parent=None, *, muted=True):
        super().__init__(parent)
        self.setCheckable(True)
        self.setMinimumSize(38, 38)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.toggled.connect(self._sync_state)
        self.set_muted(muted)

    def set_muted(self, muted):
        self.setChecked(bool(muted))
        self._sync_state()

    def _sync_state(self, *_args):
        action = '开启声音' if self.isChecked() else '静音'
        self.setToolTip(action)
        self.setAccessibleName(action)
        self.setAccessibleDescription('当前已静音' if self.isChecked() else '当前声音开启')
        self.update()

    def sizeHint(self):
        return QSize(38, 38)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        # Let the native style keep hover, focus, pressed and checked chrome.
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        group = (QPalette.ColorGroup.Disabled if not self.isEnabled()
                 else QPalette.ColorGroup.Active if option.state & QStyle.StateFlag.State_Active
                 else QPalette.ColorGroup.Inactive)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        edge = min(24, self.width() - 8, self.height() - 8)
        origin = QPointF((self.width() - edge) / 2, (self.height() - edge) / 2)
        if option.state & QStyle.StateFlag.State_Sunken:
            origin += QPointF(self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftHorizontal, option, self),
                              self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftVertical, option, self))
        painter.translate(origin)
        painter.scale(edge / 24, edge / 24)
        pen = QPen(option.palette.color(group, QPalette.ColorRole.ButtonText), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        speaker = QPainterPath(QPointF(3, 9))
        for x, y in ((7, 9), (12, 5), (12, 19), (7, 15), (3, 15)):
            speaker.lineTo(x, y)
        speaker.closeSubpath()
        painter.drawPath(speaker)
        if self.isChecked():
            painter.drawLine(QPointF(16, 9), QPointF(21, 15))
            painter.drawLine(QPointF(21, 9), QPointF(16, 15))
        else:
            waves = QPainterPath(QPointF(16, 8))
            waves.cubicTo(19, 10, 19, 14, 16, 16)
            waves.moveTo(19, 5)
            waves.cubicTo(24, 8.5, 24, 15.5, 19, 19)
            painter.drawPath(waves)
