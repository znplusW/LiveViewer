"""Native-semantics controls for settings and multi-select menus."""
import time

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QEventPoint, QMouseEvent, QPainter, QPainterPath, QPalette, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QScrollArea, QSizePolicy, QStyle, QStyleOptionButton, QWidget,
)
from shiboken6 import isValid
from .ui_theme import tokens_for


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
        colors = tokens_for(self)
        color = QColor(colors['switch_on' if self.isChecked() else 'switch_off'])
        if self.isDown():
            color = color.darker(112)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(track, self.TRACK_HEIGHT / 2, self.TRACK_HEIGHT / 2)
        diameter = self.TRACK_HEIGHT - 4
        thumb_on_right = self.isChecked() != rtl
        thumb_left = track.right() - diameter - 2 if thumb_on_right else track.left() + 2
        painter.setBrush(QColor(colors['switch_thumb']))
        painter.drawEllipse(QRectF(thumb_left, track.top() + 2, diameter, diameter))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors['focus']), 2))
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
        pen = QPen(QColor(tokens_for(self)['tick']), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        path = QPainterPath()
        path.moveTo(indicator.left() + indicator.width() * .23, indicator.top() + indicator.height() * .51)
        path.lineTo(indicator.left() + indicator.width() * .43, indicator.top() + indicator.height() * .71)
        path.lineTo(indicator.left() + indicator.width() * .79, indicator.top() + indicator.height() * .30)
        painter.drawPath(path)


class _SettingsTouchFilter(QObject):
    """Keep the app-wide observer separate from QScrollArea's viewport filter."""

    def eventFilter(self, watched, event):
        return self.parent()._filter_touch(watched, event)


class SettingsScrollArea(QScrollArea):
    """Scroll over form controls, committing a touch tap only after release.

    A native combo opens on mouse-down, before a flick recognizer can take
    over. Owning raw touch on the form avoids opening/changing controls while
    scrolling; completed taps still use the controls' native mouse handling.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self._touch_device = None
        self._touch_id = None
        self._touch_target = None
        self._touch_start = QPointF()
        self._touch_start_value = 0
        self._touch_moved = False
        self._touch_cancelled = False
        self._touch_mouse_until = 0.0
        self._replaying_tap = False
        self._touch_filter = _SettingsTouchFilter(self)
        self._touch_filter_installed = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._touch_filter_installed:
            QApplication.instance().installEventFilter(self._touch_filter)
            self._touch_filter_installed = True

    def hideEvent(self, event):
        if self._touch_filter_installed:
            QApplication.instance().removeEventFilter(self._touch_filter)
            self._touch_filter_installed = False
        self._reset_touch()
        super().hideEvent(event)

    def _belongs_to_page(self, widget):
        return (isinstance(widget, QWidget) and widget.window() is self.window()
                and (widget is self.viewport() or self.viewport().isAncestorOf(widget)))

    def _reset_touch(self):
        self._touch_device = self._touch_id = self._touch_target = None
        self._touch_cancelled = False

    def _send_tap_mouse(self, target, position, device, kind):
        if not isValid(target) or not target.isVisible() or not target.isEnabled():
            return
        local = QPointF(target.mapFromGlobal(position.toPoint()))
        window_position = QPointF(target.window().mapFromGlobal(position.toPoint()))
        event = QMouseEvent(kind, local, window_position, position, Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventSynthesizedByApplication, device)
        self._replaying_tap = True
        try:
            QApplication.sendEvent(target, event)
        finally:
            self._replaying_tap = False

    def _replay_tap(self, target, position, device):
        if target is None or not isValid(target) or not self.isVisible():
            return
        focus = target
        while focus is not None and self._belongs_to_page(focus):
            if focus.focusPolicy() & Qt.FocusPolicy.ClickFocus:
                focus.setFocus(Qt.FocusReason.MouseFocusReason)
                break
            focus = focus.parentWidget()
        # Schedule release first: a menu button can enter a nested event loop
        # while processing its press, and must still receive its button-up.
        QTimer.singleShot(0, self, lambda: self._send_tap_mouse(
            target, position, device, QEvent.Type.MouseButtonRelease))
        self._send_tap_mouse(target, position, device, QEvent.Type.MouseButtonPress)

    def _filter_touch(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate) and watched in (self, self.window()):
            self._reset_touch()
        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            if not self._belongs_to_page(watched) or not self.isVisible():
                return False
            if kind == QEvent.Type.TouchBegin:
                if QApplication.activePopupWidget() is not None:
                    return False
                points = event.points()
                if not points:
                    return False
                self._touch_device = event.pointingDevice()
                self._touch_id = points[0].id()
                self._touch_start = QPointF(points[0].globalPosition())
                target = QApplication.widgetAt(self._touch_start.toPoint())
                # Keep the Python wrapper alive until release. Internal Qt
                # editors otherwise may have no Python owner between events.
                self._touch_target = target if self._belongs_to_page(target) else None
                self._touch_start_value = self.verticalScrollBar().value()
                self._touch_moved = False
                self._touch_cancelled = len(points) != 1
            if self._touch_device != event.pointingDevice():
                return False
            self._touch_mouse_until = time.monotonic() + .7
            points = event.points()
            if kind == QEvent.Type.TouchCancel:
                self._reset_touch()
            else:
                if len(points) != 1 or any(p.id() != self._touch_id for p in points):
                    self._touch_cancelled = True
                point = next((p for p in points if p.id() == self._touch_id), None)
                if point is not None:
                    delta = point.globalPosition() - self._touch_start
                    if max(abs(delta.x()), abs(delta.y())) >= max(16, QApplication.startDragDistance()):
                        self._touch_moved = True
                    if self._touch_moved and not self._touch_cancelled:
                        self.verticalScrollBar().setValue(self._touch_start_value - round(delta.y()))
                if kind == QEvent.Type.TouchEnd or not any(p.state() != QEventPoint.State.Released for p in points):
                    tap = not self._touch_moved and not self._touch_cancelled and point is not None
                    target, position, device = self._touch_target, QPointF(self._touch_start), self._touch_device
                    self._reset_touch()
                    if tap:
                        QTimer.singleShot(0, self, lambda: self._replay_tap(target, position, device))
            event.accept()
            return True
        if (kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                     QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove)
                and not self._replaying_tap and time.monotonic() < self._touch_mouse_until
                and self._belongs_to_page(watched)
                and event.source() != Qt.MouseEventSource.MouseEventNotSynthesized):
            event.accept()
            return True
        return False

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
