"""Native selectable filenames and mouse copying of genuinely elided labels."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (QEvent, QItemSelectionModel, QModelIndex, QObject,
                            QPersistentModelIndex, QPoint, QRect, QTimer, Qt)
from PySide6.QtWidgets import QApplication, QLineEdit, QStyleOptionViewItem


def filename_tooltip(path, note=''):
    path = Path(path)
    return f'{path.name}\n{path.parent}' + (f'\n{note}' if note else '')


class FilenameField(QLineEdit):
    """Keep QLineEdit selection, horizontal scrolling and native copy controls."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setFrame(False)
        self.setTextMargins(0, 0, 0, 0)
        self.setCursorPosition(0)

    def setText(self, text):
        if text == self.text():
            return
        super().setText(text)
        self.setCursorPosition(0)

    def set_file_path(self, path):
        self.setText(Path(path).name)
        self.setToolTip(filename_tooltip(path))


@dataclass(frozen=True)
class FilenameLayout:
    original: str
    displayed: str
    draw_rect: QRect
    text_rect: QRect

    @property
    def is_elided(self):
        return self.displayed != self.original


class ElidedFilenameMouseHandler(QObject):
    """Defer only elided text clicks so their second click can still arrive.

    Views call this after their existing touch/mouse guards. No application
    event filter or synthesized touch handling is introduced here.
    """

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self._owned = None
        self._pending = None
        self._origin = QPoint()
        self._moved = False
        self._swallow_release = False
        self._cancelled_click = False
        self._model = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        # Coarse timers may fire early, before Windows' double-click window ends.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._activate)
        view.installEventFilter(self)
        view.horizontalScrollBar().valueChanged.connect(self.cancel)
        view.verticalScrollBar().valueChanged.connect(self.cancel)

    def cancel(self, *_):
        if self._pending is not None or self._owned is not None:
            self._cancelled_click = True
        self._timer.stop()
        self._pending = None
        self._owned = None
        self._moved = False

    def eventFilter(self, receiver, event):
        if event.type() in (QEvent.Type.Hide, QEvent.Type.FocusOut, QEvent.Type.Resize,
                            QEvent.Type.WindowDeactivate, QEvent.Type.EnabledChange,
                            QEvent.Type.KeyPress, QEvent.Type.ContextMenu, QEvent.Type.FontChange,
                            QEvent.Type.StyleChange, QEvent.Type.LayoutDirectionChange):
            self.cancel()
        return False

    def _bind_model(self):
        model = self.view.model()
        if model is self._model:
            return
        self.cancel()
        if self._model is not None:
            for name in ('modelAboutToBeReset', 'layoutAboutToBeChanged', 'rowsAboutToBeRemoved'):
                try:
                    getattr(self._model, name).disconnect(self.cancel)
                except (RuntimeError, TypeError):
                    pass
        self._model = model
        if model is not None:
            for name in ('modelAboutToBeReset', 'layoutAboutToBeChanged', 'rowsAboutToBeRemoved'):
                getattr(model, name).connect(self.cancel)

    def _target(self, point):
        self._bind_model()
        index = self.view.indexAt(point)
        if (not index.isValid() or not index.flags() & Qt.ItemFlag.ItemIsEnabled
                or not index.flags() & Qt.ItemFlag.ItemIsSelectable):
            return None
        delegate = self.view.itemDelegateForIndex(index)
        layout_for = getattr(delegate, 'filename_layout', None)
        if layout_for is None:
            return None
        option = QStyleOptionViewItem()
        option.initFrom(self.view)
        option.rect = self.view.visualRect(index)
        layout = layout_for(option, index)
        if layout.is_elided and layout.text_rect.contains(point):
            return QPersistentModelIndex(index), layout.original
        return None

    def _activate(self):
        target = self._pending
        self._pending = None
        if target is None or not self.view.isVisible() or not self.view.isEnabled():
            return
        index, name = target
        if (index.isValid() and index.model() is self.view.model()
                and index.data(Qt.ItemDataRole.DisplayRole) == name
                and index.flags() & Qt.ItemFlag.ItemIsEnabled
                and index.flags() & Qt.ItemFlag.ItemIsSelectable):
            self.view.clicked.emit(QModelIndex(index))

    def handle(self, event):
        kind = event.type()
        if kind in (QEvent.Type.Resize, QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                    QEvent.Type.TouchEnd, QEvent.Type.TouchCancel, QEvent.Type.Wheel,
                    QEvent.Type.ContextMenu):
            self.cancel()
            return False
        if kind not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                        QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove):
            return False
        if event.source() != Qt.MouseEventSource.MouseEventNotSynthesized:
            self.cancel()
            return False
        point = event.position().toPoint()
        if kind == QEvent.Type.MouseMove:
            if self._owned is None:
                return False
            if (point - self._origin).manhattanLength() >= QApplication.startDragDistance():
                self._moved = True
            event.accept()
            return True
        if (kind == QEvent.Type.MouseButtonRelease and self._swallow_release
                and event.button() == Qt.MouseButton.LeftButton):
            self._swallow_release = False
            target = self._owned
            self._owned = None
            if (target is not None and not self._moved
                    and event.button() == Qt.MouseButton.LeftButton
                    and event.modifiers() == Qt.KeyboardModifier.NoModifier
                    and self._target(point) == target):
                self._pending = target
                self._timer.start(QApplication.doubleClickInterval())
            event.accept()
            return True
        if (event.button() != Qt.MouseButton.LeftButton
                or event.modifiers() != Qt.KeyboardModifier.NoModifier):
            self.cancel()
            return False
        target = self._target(point)
        if kind == QEvent.Type.MouseButtonDblClick:
            if self._cancelled_click:
                # A model/lifecycle change may happen after the first release.
                # Its late second click must not become a new native press.
                self._swallow_release = True
                event.accept()
                return True
            if target is not None and target in (self._pending, self._owned):
                self.cancel()
                self._swallow_release = True
                QApplication.clipboard().setText(target[1])
                event.accept()
                return True
            self.cancel()
            return False
        if kind == QEvent.Type.MouseButtonPress:
            self.cancel()
            self._cancelled_click = False
            self._swallow_release = False
            if target is not None:
                self.view.setFocus(Qt.FocusReason.MouseFocusReason)
                self.view.selectionModel().setCurrentIndex(
                    QModelIndex(target[0]), QItemSelectionModel.SelectionFlag.ClearAndSelect)
                self._owned = target
                self._origin = point
                self._swallow_release = True
                event.accept()
                return True
        return False
