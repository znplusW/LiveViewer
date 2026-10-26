"""Compact photo information row with one optional, menu-backed action."""
from PySide6.QtCore import QEvent, QSize, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLayout


class PhotoInfoBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('infobar')
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(16, 7, 16, 7)
        self.row.setSpacing(6)
        # The optional action must not force the window wider before it can
        # move into the existing menu. Required controls keep their minimums.
        self.row.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._overflow = None
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._arrange)

    def set_overflow_widget(self, widget):
        self._overflow = widget
        self.refresh()

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        button = self._overflow
        if button is not None and self.row.indexOf(button) >= 0 and not button.isHidden():
            size.setWidth(size.width() - button.minimumSizeHint().width() - self.row.spacing())
        return size.expandedTo(QSize(0, 0))

    def refresh(self):
        self._layout_timer.start(0)

    def _arrange(self):
        # Include QFrame's styled border as well as the layout margins. A
        # splitter must not allocate less than the required touch controls.
        self.setMinimumWidth(self.minimumSizeHint().width())
        button = self._overflow
        if button is None or self.row.indexOf(button) < 0:
            return
        required = self.minimumSizeHint().width() + button.sizeHint().width() + self.row.spacing()
        # Give the filename more than its emergency minimum before restoring
        # the duplicate preview entry. The menu always keeps the same action.
        visible = self.width() >= required + 60
        if button.isHidden() == visible:
            button.setVisible(visible)
            self.updateGeometry()

    def event(self, event):
        result = super().event(event)
        if hasattr(self, '_layout_timer') and event.type() in (
                QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest,
                QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.refresh()
        return result
