"""Native-semantics controls for settings and multi-select menus."""
import time
import weakref

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QEventPoint, QInputDevice, QMouseEvent, QPainter, QPainterPath, QPalette, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QMenu, QScrollArea,
    QSizePolicy, QStyle, QStyleOptionButton, QWidget,
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
    """Application input filter independent of QScrollArea's layout filter."""

    def __init__(self, area):
        super().__init__(area)
        self._area = weakref.ref(area)

    def eventFilter(self, receiver, event):
        area = self._area()
        if area is None or not isValid(area):
            return False
        return area._filter_touch_event(receiver, event)


class SettingsScrollArea(QScrollArea):
    """Touchscreen drags and pixel-only touchpad scrolling on the settings page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self._touch_device = None
        self._touch_contacts = set()
        self._touch_primary = None
        self._touch_target = None
        self._touch_cancelled = False
        self._touch_dragged = False
        self._touch_tail_until = 0.0
        self._touch_popup = None
        self._touch_popup_origin = None
        self._replaying_touch_click = False
        self._touch_event_filter = _SettingsTouchFilter(self)
        self._touch_filter_installed = False

    def _clear_touch(self):
        self._touch_device = None
        self._touch_contacts.clear()
        self._touch_primary = None
        self._touch_target = None

    def _filter_touch_event(self, receiver, event):
        kind = event.type()
        if self._filter_popup_touch_tail(receiver, event):
            return True
        if self._touch_contacts:
            if kind == QEvent.Type.WindowDeactivate and receiver is self.window():
                self._touch_cancelled = True
            elif (kind == QEvent.Type.Show and isinstance(receiver, QWidget) and receiver.isWindow()
                  and receiver is not self.window()
                  and (receiver.windowType() == Qt.WindowType.Popup
                       or receiver.windowModality() != Qt.WindowModality.NonModal)):
                self._touch_cancelled = True
        if (kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                     QEvent.Type.TouchEnd, QEvent.Type.TouchCancel)
                and receiver is self.viewport()
                and self._handle_touch(event)):
            return True
        if (kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                     QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove)
                and not self._replaying_touch_click and isinstance(receiver, QWidget)
                and receiver.window() is self.window()
                and (receiver is self.viewport() or self.viewport().isAncestorOf(receiver))):
            if event.source() == Qt.MouseEventSource.MouseEventNotSynthesized:
                if kind == QEvent.Type.MouseButtonPress:
                    self._touch_cancelled = True
                    self._touch_tail_until = 0.0
            elif self._touch_contacts or time.monotonic() < self._touch_tail_until:
                event.accept()
                return True
        return False

    def _filter_popup_touch_tail(self, receiver, event):
        popup = self._touch_popup() if self._touch_popup is not None else None
        if popup is None or not isValid(popup) or time.monotonic() >= self._touch_tail_until:
            self._touch_popup = None
            self._touch_popup_origin = None
            return False
        kind = event.type()
        if kind == QEvent.Type.Hide and receiver is popup:
            self._touch_popup = None
            self._touch_popup_origin = None
            return False
        # Qt routes popup touches through the QWindow and then promotes them
        # to mouse events. A new contact must keep that native menu behavior.
        scoped = (receiver is popup.windowHandle() or receiver is self.window().windowHandle()
                  or isinstance(receiver, QWidget) and receiver.window() is popup)
        if not scoped:
            return False
        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate):
            if any(point.state() == QEventPoint.State.Pressed for point in event.points()):
                self._touch_popup = None
                self._touch_popup_origin = None
            return False
        if kind not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                        QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove):
            return False
        if event.source() == Qt.MouseEventSource.MouseEventNotSynthesized:
            if kind == QEvent.Type.MouseButtonPress:
                self._touch_popup = None
                self._touch_popup_origin = None
                self._touch_tail_until = 0.0
            return False
        # Only the original button's area belongs to the completed tap. Do
        # not swallow promoted mouse input over options in the open menu.
        if (not self._replaying_touch_click and self._touch_popup_origin is not None
                and self._touch_popup_origin.contains(event.globalPosition())):
            event.accept()
            return True
        return False

    def _handle_touch(self, event):
        device = event.pointingDevice()
        if device.type() != QInputDevice.DeviceType.TouchScreen:
            return False
        if self._touch_contacts and device != self._touch_device:
            return False
        if event.type() == QEvent.Type.TouchCancel:
            self._clear_touch()
            self._touch_tail_until = time.monotonic() + .8
            event.accept()
            return True
        points = event.points()
        pressed = [point for point in points if point.state() == QEventPoint.State.Pressed]
        if not self._touch_contacts:
            if not pressed or QApplication.activePopupWidget() is not None:
                return False
            point = pressed[0]
            position = self.viewport().mapFromGlobal(point.globalPosition())
            if not self.viewport().rect().contains(position.toPoint()):
                return False
            self._touch_device = device
            self._touch_primary = point.id()
            self._touch_start = point.globalPosition()
            self._touch_scroll_start = self.verticalScrollBar().value()
            child = self.viewport().childAt(position.toPoint())
            self._touch_target = weakref.ref(child) if child is not None else None
            self._touch_cancelled = False
            self._touch_dragged = False
        # Update+Pressed/Released is valid. Unknown old Stationary contacts
        # were never owned by this page and must not become a second finger.
        self._touch_contacts.update(point.id() for point in pressed)
        if len(self._touch_contacts) > 1:
            self._touch_cancelled = True
        point = next((point for point in points if point.id() == self._touch_primary), None)
        if point is not None and not self._touch_cancelled:
            delta = point.globalPosition() - self._touch_start
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._touch_dragged = True
            if self._touch_dragged:
                self.verticalScrollBar().setValue(self._touch_scroll_start - round(delta.y()))
        for released in points:
            if released.state() == QEventPoint.State.Released:
                self._touch_contacts.discard(released.id())
        self._touch_tail_until = time.monotonic() + .8
        event.accept()
        if not self._touch_contacts:
            target = self._touch_target() if self._touch_target is not None else None
            click = (point is not None and point.state() == QEventPoint.State.Released
                     and not self._touch_cancelled and not self._touch_dragged)
            self._clear_touch()
            if click:
                self._send_touch_click(target, point.globalPosition(), event.modifiers())
        return True

    def _send_touch_click(self, target, global_position, modifiers):
        if (target is None or not isValid(target) or not target.isVisible() or not target.isEnabled()
                or not target.rect().contains(target.mapFromGlobal(global_position).toPoint())):
            return
        if target.focusPolicy() & Qt.FocusPolicy.ClickFocus:
            target.setFocus(Qt.FocusReason.MouseFocusReason)
        self._replaying_touch_click = True
        try:
            for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                if not isValid(target):
                    break
                mouse = QMouseEvent(kind, target.mapFromGlobal(global_position),
                    target.window().mapFromGlobal(global_position), global_position,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                    modifiers, Qt.MouseEventSource.MouseEventSynthesizedByApplication)
                QApplication.sendEvent(target, mouse)
        finally:
            self._replaying_touch_click = False
        popup = QApplication.activePopupWidget()
        if (isValid(target) and isinstance(popup, QMenu) and popup.parentWidget() is target):
            # The replay opens a different window. Its promoted mouse tail
            # would otherwise escape the viewport-only guard above and close
            # the menu before the next physical tap can select an option.
            self._touch_popup = weakref.ref(popup)
            self._touch_popup_origin = QRectF(target.rect())
            self._touch_popup_origin.moveTopLeft(target.mapToGlobal(QPointF(0, 0)))

    def showEvent(self, event):
        super().showEvent(event)
        # Settings dialogs remain parent-owned after exec(). Only visible
        # pages observe promoted mouse tails, before a child can act on them.
        if not self._touch_filter_installed:
            QApplication.instance().installEventFilter(self._touch_event_filter)
            self._touch_filter_installed = True

    def hideEvent(self, event):
        if self._touch_filter_installed:
            QApplication.instance().removeEventFilter(self._touch_event_filter)
            self._touch_filter_installed = False
        self._clear_touch()
        self._touch_popup = None
        self._touch_popup_origin = None
        self._touch_tail_until = time.monotonic() + .8
        super().hideEvent(event)

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
