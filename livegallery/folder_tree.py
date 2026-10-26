"""Independent folder checkboxes with batched mouse and keyboard selection."""
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, QSignalBlocker, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QRubberBand, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem)

from .library import natural_key
from .ui_theme import tokens_for


class _FolderDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width() + 16, max(38, size.height()))

    def paint(self, painter, option, index):
        box = QStyleOptionViewItem(option)
        self.initStyleOption(box, index)
        widget = option.widget
        rect = widget.checkbox_rect(index)
        colors = tokens_for(widget)
        # Paint one indicator only. Native item focus/check rendering can
        # otherwise leave blue corner fragments outside the custom checkbox.
        native = QStyleOptionViewItem(box)
        native.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        native.state &= ~QStyle.StateFlag.State_HasFocus
        native.text = ''
        widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, native, painter, widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = box.checkState != Qt.CheckState.Unchecked
        painter.setBrush(QColor(colors['switch_on'] if selected else colors['field']))
        painter.setPen(QPen(QColor(colors['switch_on'] if selected else colors['control_button_border']), 1.3))
        shape = QRectF(rect).adjusted(.7, .7, -.7, -.7)
        painter.drawRoundedRect(shape, 2, 2)
        if selected:
            painter.setPen(QPen(QColor('#ffffff'), 2.5, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath()
            if box.checkState == Qt.CheckState.PartiallyChecked:
                path.moveTo(shape.left() + shape.width() * .25, shape.center().y())
                path.lineTo(shape.left() + shape.width() * .75, shape.center().y())
            else:
                path.moveTo(shape.left() + shape.width() * .2, shape.top() + shape.height() * .53)
                path.lineTo(shape.left() + shape.width() * .42, shape.top() + shape.height() * .76)
                path.lineTo(shape.left() + shape.width() * .82, shape.top() + shape.height() * .26)
            painter.drawPath(path)
        text_rect = option.rect.adjusted(rect.right() - option.rect.left() + 9, 0, -6, 0)
        painter.setFont(box.font)
        painter.setPen(QColor(colors['selected_text'] if box.state & QStyle.StateFlag.State_Selected else colors['text']))
        text = box.fontMetrics.elidedText(box.text, Qt.TextElideMode.ElideRight, max(0, text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()


class FolderTree(QTreeWidget):
    checkedChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setUniformRowHeights(True)
        self.setItemDelegate(_FolderDelegate(self))
        self.setAccessibleName('已选文件夹')
        self.setToolTip('每个勾选框只控制本层图片。点击名称记录范围起点；Shift 点选范围，按住 Ctrl 拖框反选，Ctrl+A 全选。')
        self._nodes = {}
        self._checked = set()
        self._anchor = None
        self.all_item = None
        self._pointer = None
        self._native_branch = False
        self._suppress_release = False
        self._generation = 0
        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self._selection_palette_timer = QTimer(self)
        self._selection_palette_timer.setSingleShot(True)
        self._selection_palette_timer.timeout.connect(self._apply_selection_palette)
        self._apply_selection_palette()
        self.itemChanged.connect(self._item_changed)
        self.itemExpanded.connect(self.cancel_gesture)
        self.itemCollapsed.connect(self.cancel_gesture)
        self.verticalScrollBar().valueChanged.connect(self.cancel_gesture)
        self.horizontalScrollBar().valueChanged.connect(self.cancel_gesture)
        for signal in (self.model().rowsAboutToBeInserted, self.model().rowsAboutToBeRemoved,
                       self.model().modelAboutToBeReset, self.model().layoutAboutToBeChanged):
            signal.connect(self.cancel_gesture)

    def clear(self):
        self.cancel_gesture()
        with QSignalBlocker(self):
            super().clear()
        self._nodes = {}
        self._checked = set()
        self.all_item = None

    def checkbox_rect(self, item_or_index):
        index = self.indexFromItem(item_or_index) if isinstance(item_or_index, QTreeWidgetItem) else item_or_index
        row = self.visualRect(index)
        return QRect(row.left() + 6, row.center().y() - 11, 24, 24)

    def cancel_gesture(self, *_args):
        self._generation += 1
        if self._pointer is not None:
            self._suppress_release = True
        self._pointer = None
        self._rubber_band.hide()

    def _queue_selection_palette(self):
        # Parent QSS repolish is still traversing widgets during these events.
        # Let that stack return before changing a child's native palette.
        if (not getattr(self, '_applying_selection_palette', False)
                and not self._selection_palette_timer.isActive()):
            self._selection_palette_timer.start(0)

    @Slot()
    def _apply_selection_palette(self):
        if getattr(self, '_applying_selection_palette', False):
            return
        colors = tokens_for(self)
        palette = self.palette()
        if (palette.color(QPalette.ColorRole.Highlight).name() == colors['selected']
                and palette.color(QPalette.ColorRole.HighlightedText).name() == colors['selected_text']):
            return
        self._applying_selection_palette = True
        try:
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
                palette.setColor(group, QPalette.ColorRole.Highlight, QColor(colors['selected']))
                palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor(colors['selected_text']))
            self.setPalette(palette)
        finally:
            self._applying_selection_palette = False

    @property
    def checked_directories(self):
        return frozenset(self._checked)

    @property
    def known_directories(self):
        return frozenset(self._nodes)

    @property
    def selection_spec(self):
        return None if self._checked == set(self._nodes) else tuple(sorted(self._checked, key=natural_key))

    def removal_targets(self, item):
        """Checked context rows remove that selection; unchecked rows stand alone."""
        if item is None:
            return ()
        if item is self.all_item:
            targets = set(self._checked)
        else:
            value = item.data(0, Qt.ItemDataRole.UserRole)
            if not value:
                return ()
            path = Path(value)
            targets = set(self._checked) if path in self._checked else {path}
        branches = []
        for path in sorted(targets, key=lambda value: (len(value.parts), natural_key(value))):
            if not any(parent in targets for parent in path.parents):
                branches.append(path)
        return tuple(sorted(branches, key=natural_key))

    def removal_count(self, targets):
        targets = set(targets)
        return sum(path in targets or any(parent in targets for parent in path.parents) for path in self._nodes)

    def set_directories(self, roots, counts, checked=None, *, include=None):
        roots = tuple(Path(root) for root in roots)
        direct = {Path(path): count for path, count in counts.items()}
        known = set(roots) | set(direct)
        for path in tuple(known):
            ancestors = [root for root in roots if path.is_relative_to(root)]
            if ancestors:
                boundary = min(ancestors, key=lambda root: len(root.parts))
                parent = path.parent
                while parent != path and parent.is_relative_to(boundary):
                    if include is None or include(parent):
                        known.add(parent)
                    if parent == boundary:
                        break
                    parent = parent.parent
        current = self.currentItem()
        selected = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        anchor = self._anchor
        desired = set(known) if checked is None else set(map(Path, checked)) & known
        if self.all_item is not None and known == set(self._nodes):
            # Background pairing often changes only the direct counts. Do not
            # rebuild or scroll the rows underneath the user's pointer.
            with QSignalBlocker(self):
                self.all_item.setText(0, f'全部文件夹  ·  {sum(direct.values()):,}')
                for path, item in self._nodes.items():
                    item.setText(0, f'{path.name or str(path)}  ·  {direct.get(path, 0):,}')
                if desired != self._checked:
                    self.cancel_gesture()
                self._checked = desired
                self._sync_checks()
            return
        self.cancel_gesture()
        expanded = {path for path, item in self._nodes.items() if item.isExpanded()}
        previous_paths = set(self._nodes)
        aggregate_expanded = self.all_item is None or self.all_item.isExpanded()
        top = self.itemAt(QPoint(self.viewport().width() // 2, 0))
        top_path = top.data(0, Qt.ItemDataRole.UserRole) if top is not None else None
        old_scroll = self.verticalScrollBar().value()
        with QSignalBlocker(self):
            self.clear()
            self.all_item = QTreeWidgetItem([f'全部文件夹  ·  {sum(direct.values()):,}'])
            self.all_item.setFlags(self.all_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self.all_item.setData(0, Qt.ItemDataRole.UserRole, None)
            self.all_item.setToolTip(0, '选择或清空全部文件夹，包括已折叠的子文件夹。')
            self.addTopLevelItem(self.all_item)
            for path in sorted(known, key=lambda path: (len(path.parts), natural_key(path))):
                parent = self._nodes.get(path.parent, self.all_item)
                item = QTreeWidgetItem([f'{path.name or str(path)}  ·  {direct.get(path, 0):,}'])
                item.setData(0, Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(0, str(path) + '\n只控制本层图片；子文件夹可单独勾选。')
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                parent.addChild(item)
                self._nodes[path] = item
            self._checked = desired
            self._sync_checks()
            self.all_item.setExpanded(aggregate_expanded)
            for path in expanded | (set(roots) - previous_paths):
                node = self._nodes.get(path)
                if node is not None:
                    node.setExpanded(True)
            self.setCurrentItem(self._nodes.get(Path(selected)) if selected else self.all_item)
            self.doItemsLayout()
            self.verticalScrollBar().setValue(old_scroll)
            if top_path and Path(top_path) in self._nodes:
                self.scrollToItem(self._nodes[Path(top_path)], QAbstractItemView.ScrollHint.PositionAtTop)
        self._anchor = anchor if anchor in known else None

    def _sync_checks(self):
        with QSignalBlocker(self):
            for path, item in self._nodes.items():
                item.setCheckState(0, Qt.CheckState.Checked if path in self._checked else Qt.CheckState.Unchecked)
            if self.all_item is not None:
                state = (Qt.CheckState.Checked if self._checked == set(self._nodes)
                         else Qt.CheckState.PartiallyChecked if self._checked else Qt.CheckState.Unchecked)
                self.all_item.setCheckState(0, state)

    def set_checked_directories(self, paths):
        selected = set(self._nodes) if paths is None else set(map(Path, paths)) & set(self._nodes)
        if selected == self._checked:
            return
        self.cancel_gesture()
        self._checked = selected
        self._sync_checks()
        self.checkedChanged.emit(self.selection_spec)

    def set_all_checked(self, checked=True):
        self.set_checked_directories(None if checked else ())

    def _item_changed(self, item, column):
        if column != 0:
            return
        if item is self.all_item:
            self.set_all_checked(item.checkState(0) == Qt.CheckState.Checked)
            return
        value = item.data(0, Qt.ItemDataRole.UserRole)
        if not value:
            return
        path = Path(value)
        selected = set(self._checked)
        if item.checkState(0) == Qt.CheckState.Checked:
            selected.add(path)
        else:
            selected.discard(path)
        self.set_checked_directories(selected)

    def _visible_paths(self):
        visible = []
        def visit(item):
            for number in range(item.childCount()):
                child = item.child(number)
                if child.isHidden():
                    continue
                visible.append(Path(child.data(0, Qt.ItemDataRole.UserRole)))
                if child.isExpanded():
                    visit(child)
        if self.all_item is not None and self.all_item.isExpanded():
            visit(self.all_item)
        return visible

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            self.cancel_gesture()
            super().mousePressEvent(event)
            return
        self.cancel_gesture()
        self._suppress_release = False
        self._native_branch = False
        point = event.position().toPoint()
        item = self.itemAt(point)
        if item is not None and point.x() < self.visualItemRect(item).left():
            self._native_branch = True
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        path = Path(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None and item is not self.all_item else None
        kind = 'blank'
        if item is not None:
            if self.checkbox_rect(item).contains(point):
                kind = 'checkbox'
            else:
                label_end = self.checkbox_rect(item).right() + 9 + self.fontMetrics().horizontalAdvance(item.text(0))
                kind = 'text' if point.x() <= label_end else 'blank'
        self._pointer = {'start': point, 'kind': kind, 'path': path, 'aggregate': item is self.all_item,
                         'baseline': set(self._checked), 'modifiers': event.modifiers(),
                         'generation': self._generation, 'dragged': False, 'cancel_click': False}
        event.accept()

    def mouseMoveEvent(self, event):
        gesture = self._pointer
        if gesture is None:
            super().mouseMoveEvent(event)
            return
        can_band = (gesture['kind'] in ('text', 'blank')
                    and gesture['modifiers'] & Qt.KeyboardModifier.ControlModifier)
        if can_band and not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.cancel_gesture()
            event.accept()
            return
        point = event.position().toPoint()
        if (point - gesture['start']).manhattanLength() >= QApplication.startDragDistance():
            if can_band:
                gesture['dragged'] = True
            else:
                gesture['cancel_click'] = True
        if gesture['dragged']:
            self._rubber_band.setGeometry(QRect(gesture['start'], point).normalized().intersected(self.viewport().rect()))
            self._rubber_band.show()
        event.accept()

    def _commit_click(self, gesture, point):
        if gesture['kind'] == 'blank':
            return
        path = gesture['path']
        if gesture['aggregate']:
            if self.itemAt(point) is self.all_item:
                self.setCurrentItem(self.all_item)
                if gesture['kind'] == 'checkbox':
                    self.set_all_checked(gesture['baseline'] != set(self._nodes))
            return
        if path is None or path not in self._nodes or self.itemAt(point) is not self._nodes[path]:
            return
        self.setCurrentItem(self._nodes[path])
        modifiers, baseline = gesture['modifiers'], gesture['baseline']
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            visible = self._visible_paths()
            anchor = self._anchor if self._anchor in visible else path
            first, last = sorted((visible.index(anchor), visible.index(path)))
            selected = set(visible[first:last + 1])
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                selected |= baseline
        else:
            self._anchor = path
            if gesture['kind'] != 'checkbox':
                return
            selected = set(baseline)
            if path in selected:
                selected.remove(path)
            else:
                selected.add(path)
        self.set_checked_directories(selected)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._native_branch:
            self._native_branch = False
            super().mouseReleaseEvent(event)
            return
        gesture, self._pointer = self._pointer, None
        self._rubber_band.hide()
        if self._suppress_release:
            self._suppress_release = False
            event.accept()
            return
        if gesture is not None and gesture['generation'] == self._generation:
            point = event.position().toPoint()
            if gesture['dragged']:
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    band = QRect(gesture['start'], point).normalized().intersected(self.viewport().rect())
                    covered = {path for path in self._visible_paths()
                               if self.visualItemRect(self._nodes[path]).intersects(band)}
                    self.set_checked_directories(gesture['baseline'] ^ covered)
            elif not gesture['cancel_click']:
                self._commit_click(gesture, point)
        # Every custom left sequence is consumed in full. A late release must
        # never fall through to Qt's independent checkbox editor.
        event.accept()

    def event(self, event):
        if hasattr(self, '_selection_palette_timer') and event.type() in (
                QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange, QEvent.Type.StyleChange):
            self._queue_selection_palette()
        if hasattr(self, '_rubber_band') and event.type() in (
                QEvent.Type.WindowDeactivate, QEvent.Type.Hide, QEvent.Type.FontChange,
                QEvent.Type.StyleChange, QEvent.Type.DevicePixelRatioChange, QEvent.Type.ScreenChangeInternal):
            self.cancel_gesture()
        return super().event(event)

    def focusOutEvent(self, event):
        self.cancel_gesture()
        super().focusOutEvent(event)

    def resizeEvent(self, event):
        if hasattr(self, '_rubber_band'):
            self.cancel_gesture()
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        self.cancel_gesture()
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.set_all_checked(True)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        gesture = self._pointer
        if (event.key() == Qt.Key.Key_Control and gesture is not None
                and gesture['kind'] in ('text', 'blank')
                and gesture['modifiers'] & Qt.KeyboardModifier.ControlModifier):
            self.cancel_gesture()
        super().keyReleaseEvent(event)
