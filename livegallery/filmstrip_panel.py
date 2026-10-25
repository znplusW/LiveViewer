"""Reusable bottom/left/right filmstrip without owning application settings."""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QListView, QPushButton, QStyle, QVBoxLayout, QWidget)

from .catalog_model import ThumbnailDelegate
from .touch_filmstrip import FilmstripResizeHandle, TouchFilmstrip


class FilmstripPanel(QFrame):
    hideRequested = Signal()
    layoutRequested = Signal(str, str)
    thicknessRequested = Signal(str, int)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.setObjectName('film')
        self.orientation = 'horizontal'
        self.side = 'right'
        self._pending_anchor = None
        self._layout_dirty = True
        self._outer = QGridLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self.resize_handle = FilmstripResizeHandle(self)
        self.resize_handle.thicknessRequested.connect(
            lambda value: self.thicknessRequested.emit(self.orientation, value))
        self._body = QWidget(self)
        body = QVBoxLayout(self._body)
        body.setContentsMargins(8, 3, 8, 5)
        body.setSpacing(3)
        buttons = QHBoxLayout()
        self._button_layout = buttons
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(5)
        buttons.addStretch()
        self.hide_button = self._button('关闭胶片条', font_size=21)
        self.hide_button.setObjectName('filmCollapse')
        self.hide_button.clicked.connect(self.hideRequested)
        buttons.addWidget(self.hide_button)
        self.orientation_button = self._button('切换胶片条方向')
        self.orientation_button.clicked.connect(self._request_orientation)
        buttons.addWidget(self.orientation_button)
        self.side_button = self._button('切换胶片条左右位置')
        self.side_button.clicked.connect(self._request_side)
        buttons.addWidget(self.side_button)
        buttons.addStretch()
        body.addLayout(buttons)
        self._info = QWidget(self._body)
        self._info_layout = QGridLayout(self._info)
        self._info_layout.setContentsMargins(0, 0, 0, 0)
        self._info_layout.setHorizontalSpacing(6)
        self._info_layout.setVerticalSpacing(0)
        self.library_label = QLabel('尚未加载照片')
        self.library_label.setObjectName('muted')
        self.analysis_label = QLabel('按需加载 · 本机缓存')
        self.analysis_label.setObjectName('muted')
        for label in (self.library_label, self.analysis_label):
            label.setStyleSheet('font-size:11px;')
        body.addWidget(self._info)
        self.strip = TouchFilmstrip()
        self.strip.setModel(model)
        self.strip.setItemDelegate(ThumbnailDelegate(self.strip))
        self.strip.setViewMode(QListView.ViewMode.IconMode)
        self.strip.setMovement(QListView.Movement.Static)
        self.strip.setResizeMode(QListView.ResizeMode.Adjust)
        self.strip.setWrapping(False)
        self.strip.setUniformItemSizes(True)
        self.strip.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.strip.setSpacing(0)
        self.strip.setMinimumSize(64, 64)
        body.addWidget(self.strip, 1)
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.timeout.connect(self._relayout)
        self.strip.viewportSizeChanged.connect(self._queue_relayout)
        self._configure()

    @staticmethod
    def _button(name, font_size=13):
        button = QPushButton()
        button.setFixedSize(38, 34)
        # QSS dimensions are the content box. Keep its 1px border explicit:
        # a smaller min-height overrides setFixedSize again on style polish.
        button.setStyleSheet(
            'QPushButton { padding:0;min-width:36px;max-width:36px;min-height:32px;max-height:32px;'
            'border-width:1px;border-style:solid;border-radius:5px;'
            f'font-size:{font_size}px; }}')
        button.setAccessibleName(name)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _request_orientation(self):
        if self.orientation == 'horizontal':
            self.layoutRequested.emit('vertical', 'right')
        else:
            self.layoutRequested.emit('horizontal', self.side)

    def _request_side(self):
        self.layoutRequested.emit('vertical', 'left' if self.orientation == 'horizontal' or self.side == 'right' else 'right')

    def set_info_visible(self, visible):
        self._info.setVisible(bool(visible))

    def set_top_inset(self, pixels):
        """Reserve a fixed display-corner overlay without changing preferences."""
        pixels = max(0, int(pixels))
        if self._outer.contentsMargins().top() == pixels:
            return
        self._remember_anchor()
        self.strip.cancel_gesture()
        self.resize_handle.cancel_gesture()
        self._outer.setContentsMargins(0, pixels, 0, 0)
        self._queue_relayout()

    def set_mode(self, orientation, side='right'):
        if orientation not in ('horizontal', 'vertical') or side not in ('left', 'right'):
            raise ValueError('胶片条布局必须为 horizontal/vertical 和 left/right。')
        if (orientation, side) == (self.orientation, self.side):
            return
        self._remember_anchor()
        self.strip.cancel_gesture()
        self.resize_handle.cancel_gesture()
        self.orientation, self.side = orientation, side
        self._configure()

    def _configure(self):
        horizontal = self.orientation == 'horizontal'
        self.strip.setLayoutDirection(Qt.LayoutDirection.RightToLeft
                                      if not horizontal and self.side == 'left'
                                      else Qt.LayoutDirection.LeftToRight)
        while self._button_layout.count():
            self._button_layout.takeAt(0)
        self._button_layout.addStretch()
        order = ((self.side_button, self.hide_button, self.orientation_button) if horizontal else
                 (self.side_button, self.orientation_button, self.hide_button) if self.side == 'right' else
                 (self.hide_button, self.orientation_button, self.side_button))
        for button in order:
            self._button_layout.addWidget(button)
        self._button_layout.addStretch()
        self.strip.set_scroll_axis(self.orientation)
        self.strip.setFlow(QListView.Flow.LeftToRight if horizontal else QListView.Flow.TopToBottom)
        self.strip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if horizontal else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if horizontal else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.hide_button.setText('X')
        self.hide_button.setToolTip('关闭胶片条')
        self.orientation_button.setText('右侧' if horizontal else '底部')
        self.orientation_button.setToolTip('移到照片右侧' if horizontal else '移到照片底部')
        self.side_button.setText('左侧' if horizontal or self.side == 'right' else '右侧')
        self.side_button.setToolTip('移到照片左侧' if horizontal or self.side == 'right' else '移到照片右侧')
        self.side_button.setVisible(True)
        self._outer.removeWidget(self.resize_handle)
        self._outer.removeWidget(self._body)
        for number in (0, 1):
            self._outer.setRowStretch(number, 0)
            self._outer.setColumnStretch(number, 0)
        if horizontal:
            self.resize_handle.set_edge('top')
            self._outer.addWidget(self.resize_handle, 0, 0)
            self._outer.addWidget(self._body, 1, 0)
            self._outer.setRowStretch(1, 1)
            self._outer.setColumnStretch(0, 1)
        else:
            edge = 'right' if self.side == 'left' else 'left'
            self.resize_handle.set_edge(edge)
            body_column = 0 if self.side == 'left' else 1
            self._outer.addWidget(self._body, 0, body_column)
            self._outer.addWidget(self.resize_handle, 0, 1 - body_column)
            self._outer.setRowStretch(0, 1)
            self._outer.setColumnStretch(body_column, 1)
        for label in (self.library_label, self.analysis_label):
            self._info_layout.removeWidget(label)
        self._info_layout.addWidget(self.library_label, 0, 0)
        self._info_layout.addWidget(self.analysis_label, 0 if horizontal else 1, 1 if horizontal else 0)
        self._info_layout.setColumnStretch(0, 1)
        self._info_layout.setColumnStretch(1, 0)
        self._layout_dirty = True
        self._queue_relayout()

    def set_extent(self, thickness):
        self._remember_anchor()
        self.strip.cancel_gesture()
        if self.orientation == 'horizontal':
            self.setMinimumWidth(0)
            self.setMaximumWidth(16777215)
            self.setFixedHeight(int(thickness))
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.setFixedWidth(int(thickness))
        self._queue_relayout()

    def capture_anchor(self):
        model = self.strip.model()
        if model is None or model.rowCount() == 0:
            return None
        horizontal = self.strip.scroll_axis == 'horizontal'
        center = self.strip.viewport().rect().center()
        index = self.strip.indexAt(center)
        if not index.isValid():
            size = self.strip.itemDelegate().sizeHint(None, None)
            extent = max(1, size.width() if horizontal else size.height())
            coordinate = center.x() if horizontal else center.y()
            row = (self.strip.current_scrollbar().value() + coordinate) // extent
            index = model.index(min(model.rowCount() - 1, max(0, row)), 0)
        rect = self.strip.visualRect(index)
        extent = self.strip.viewport().width() if horizontal else self.strip.viewport().height()
        coordinate = rect.center().x() if horizontal else rect.center().y()
        return {'index': QPersistentModelIndex(index), 'fraction': coordinate / max(1, extent)}

    def _remember_anchor(self):
        if self._pending_anchor is None:
            self._pending_anchor = self.capture_anchor()

    def restore_anchor(self, anchor):
        if anchor is not None:
            self._pending_anchor = anchor
            self._queue_relayout()

    def _queue_relayout(self, *_args):
        if hasattr(self, '_relayout_timer'):
            self._relayout_timer.start(0)

    def _relayout(self):
        anchor = self._pending_anchor or self.capture_anchor()
        self._pending_anchor = None
        delegate = self.strip.itemDelegate()
        scrollbar = self.strip.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        frame = 2 * self.strip.frameWidth()
        if self.orientation == 'horizontal':
            height = max(64, self.strip.height() - frame - scrollbar - 4)
        else:
            width = max(80, self.strip.width() - frame - scrollbar - 4)
            height = max(64, int(width * 116 / 144))
        changed = delegate.set_thumbnail_height(height)
        if changed or self._layout_dirty:
            self.strip.cancel_gesture()
            self.strip.setGridSize(delegate.sizeHint(None, None))
            self.strip.doItemsLayout()
            self._layout_dirty = False
        if anchor is not None and anchor['index'].isValid():
            index = QModelIndex(anchor['index'])
            rect = self.strip.visualRect(index)
            horizontal = self.orientation == 'horizontal'
            extent = self.strip.viewport().width() if horizontal else self.strip.viewport().height()
            coordinate = rect.center().x() if horizontal else rect.center().y()
            bar = self.strip.current_scrollbar()
            bar.setValue(bar.value() + round(coordinate - extent * anchor['fraction']))
        self.strip.viewport().update()

    def resizeEvent(self, event):
        if hasattr(self, 'strip'):
            self._remember_anchor()
            self.strip.cancel_gesture()
        super().resizeEvent(event)
        self._queue_relayout()

    def hideEvent(self, event):
        self.strip.cancel_gesture()
        self.resize_handle.cancel_gesture()
        super().hideEvent(event)
