"""Independent checkboxes for the edges that may exit fullscreen."""
from PySide6.QtCore import QEvent, QSignalBlocker, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QMenu, QPushButton, QWidgetAction

from .settings_controls import TickCheckBox
from .ui_theme import tokens_for


GESTURES = (('top', '上沿向下滑'), ('left', '左侧边缘向右滑'), ('right', '右侧边缘向左滑'))


class _GestureMenu(QMenu):
    def keyPressEvent(self, event):
        action = self.activeAction()
        if event.key() == Qt.Key.Key_Space and isinstance(action, QWidgetAction):
            checkbox = action.defaultWidget()
            if checkbox is not None and checkbox.isEnabled():
                checkbox.click()
            event.accept()
            return
        super().keyPressEvent(event)


class FullscreenGestureSelector(QPushButton):
    selectionChanged = Signal(object)

    def __init__(self, values=('left',), parent=None):
        super().__init__(parent)
        self.setAccessibleName('退出全屏手势')
        self.setMinimumWidth(200)
        self.setMinimumHeight(36)
        self.menu = _GestureMenu(self)
        self.menu.setMinimumWidth(250)
        self.setMenu(self.menu)
        self.checkboxes = {}
        self.actions_by_gesture = {}
        self._option_style_timer = QTimer(self)
        self._option_style_timer.setSingleShot(True)
        self._option_style_timer.timeout.connect(self._apply_option_style)
        for key, label in GESTURES:
            checkbox = TickCheckBox(label, self.menu)
            checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            checkbox.setMinimumHeight(38)
            action = QWidgetAction(self.menu)
            action.setDefaultWidget(checkbox)
            self.menu.addAction(action)
            self.checkboxes[key] = checkbox
            self.actions_by_gesture[key] = action
            checkbox.toggled.connect(self._changed)
        self._queue_option_style()
        self.set_selected_gestures(values)

    def selected_gestures(self):
        return [key for key, _label in GESTURES if self.checkboxes[key].isChecked()]

    def set_selected_gestures(self, values):
        selected = set(values) if isinstance(values, (list, tuple, set, frozenset)) else {'left'}
        for key, checkbox in self.checkboxes.items():
            with QSignalBlocker(checkbox):
                checkbox.setChecked(key in selected)
        self._update_summary()

    def _changed(self, _checked):
        self._update_summary()
        self.selectionChanged.emit(self.selected_gestures())

    def _update_summary(self):
        labels = [label for key, label in GESTURES if self.checkboxes[key].isChecked()]
        self._summary = '、'.join(labels) if labels else '不启用退出手势'
        self.setToolTip(self._summary + '\n可多选；全部取消可关闭手势退出。Esc／F11 仍可退出全屏。')
        self.setAccessibleDescription(self._summary)
        self.setText(self.fontMetrics().elidedText(self._summary, Qt.TextElideMode.ElideRight,
                                                  max(1, self.width() - 36)))

    def _queue_option_style(self):
        # Parent QSS propagation is still on the Qt stack during changeEvent.
        # A child-owned timer both coalesces that burst and dies with the widget.
        if not self._option_style_timer.isActive():
            self._option_style_timer.start(0)

    @Slot()
    def _apply_option_style(self):
        colors = tokens_for(self)
        style = (f"QCheckBox {{color:{colors['text']};padding:7px 12px;spacing:9px;background:transparent;}}"
                 f"QCheckBox:hover {{background:{colors['hover']};border-radius:4px;}}"
                 f"QCheckBox::indicator {{width:18px;height:18px;border:1px solid {colors['control_button_border']};"
                 f"border-radius:3px;background:{colors['field']};}}"
                 f"QCheckBox::indicator:checked {{background:{colors['switch_on']};border:1px solid {colors['switch_on']};}}"
                 f"QCheckBox:disabled {{color:{colors['muted']};}}")
        for checkbox in self.checkboxes.values():
            if checkbox.styleSheet() != style:
                checkbox.setStyleSheet(style)
        self._update_summary()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, '_option_style_timer') and event.type() in (
                QEvent.Type.StyleChange, QEvent.Type.PaletteChange, QEvent.Type.FontChange):
            self._queue_option_style()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_summary'):
            self._update_summary()

    def wheelEvent(self, event):
        # SettingsWheelGuard forwards this control's closed wheel to its page.
        # Without a settings-page parent, leave ordinary QWidget propagation.
        event.ignore()
