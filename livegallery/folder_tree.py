"""Independent folder checkboxes with batched mouse and keyboard selection."""
from pathlib import Path

from PySide6.QtCore import QRectF, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget, QTreeWidgetItem

from .library import natural_key
from .ui_theme import tokens_for


class _FolderDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.data(Qt.ItemDataRole.CheckStateRole) is None:
            return
        box = QStyleOptionViewItem(option)
        self.initStyleOption(box, index)
        widget = option.widget
        rect = widget.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, box, widget)
        colors = tokens_for(widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Cover the platform indicator before drawing a consistent hollow
        # checkbox or a visible white tick in either application theme.
        painter.fillRect(rect, QColor(colors['field']))
        selected = box.checkState != Qt.CheckState.Unchecked
        painter.setBrush(QColor(colors['switch_on'] if selected else colors['field']))
        painter.setPen(QPen(QColor(colors['switch_on'] if selected else colors['control_button_border']), 1.3))
        shape = QRectF(rect).adjusted(.7, .7, -.7, -.7)
        painter.drawRoundedRect(shape, 2, 2)
        if selected:
            painter.setPen(QPen(QColor('#ffffff'), 2, Qt.PenStyle.SolidLine,
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
        painter.restore()


class FolderTree(QTreeWidget):
    checkedChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setItemDelegate(_FolderDelegate(self))
        self.setAccessibleName('已选文件夹')
        self.setToolTip('每个勾选框只控制本层图片。Shift 选择范围，Ctrl+A 选择全部文件夹。')
        self._nodes = {}
        self._checked = set()
        self._anchor = None
        self.all_item = None
        self.itemChanged.connect(self._item_changed)

    def clear(self):
        with QSignalBlocker(self):
            super().clear()
        self._nodes = {}
        self._checked = set()
        self.all_item = None

    @property
    def checked_directories(self):
        return frozenset(self._checked)

    @property
    def known_directories(self):
        return frozenset(self._nodes)

    @property
    def selection_spec(self):
        return None if self._checked == set(self._nodes) else tuple(sorted(self._checked, key=natural_key))

    def set_directories(self, roots, counts, checked=None):
        roots = tuple(Path(root) for root in roots)
        direct = {Path(path): count for path, count in counts.items()}
        known = set(roots) | set(direct)
        for path in tuple(known):
            ancestors = [root for root in roots if path.is_relative_to(root)]
            if ancestors:
                boundary = min(ancestors, key=lambda root: len(root.parts))
                parent = path.parent
                while parent != path and parent.is_relative_to(boundary):
                    known.add(parent)
                    if parent == boundary:
                        break
                    parent = parent.parent
        current = self.currentItem()
        selected = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        anchor = self._anchor
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
            self._checked = set(known) if checked is None else set(map(Path, checked)) & known
            self._sync_checks()
            self.all_item.setExpanded(True)
            for root in roots:
                node = self._nodes.get(root)
                if node is not None:
                    node.setExpanded(True)
            self.setCurrentItem(self._nodes.get(Path(selected)) if selected else self.all_item)
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
        item = self.itemAt(event.position().toPoint())
        if event.button() != Qt.MouseButton.LeftButton or item is None:
            super().mousePressEvent(event)
            return
        rect = self.visualItemRect(item)
        if event.position().x() < rect.left():
            # Keep the native expand/collapse branch control.
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.setCurrentItem(item)
        if item is self.all_item:
            self.set_all_checked(self._checked != set(self._nodes))
            event.accept()
            return
        path = Path(item.data(0, Qt.ItemDataRole.UserRole))
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            visible = self._visible_paths()
            anchor = self._anchor if self._anchor in visible else path
            first, last = sorted((visible.index(anchor), visible.index(path)))
            selected = set(visible[first:last + 1])
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                selected |= self._checked
        else:
            option = QStyleOptionViewItem()
            self.itemDelegate().initStyleOption(option, self.indexFromItem(item))
            option.rect = rect
            checkbox = self.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                                   option, self.viewport())
            toggle = checkbox.contains(event.position().toPoint()) or modifiers & Qt.KeyboardModifier.ControlModifier
            selected = set(self._checked) if toggle else set()
            if toggle and path in selected:
                selected.remove(path)
            else:
                selected.add(path)
            self._anchor = path
        self.set_checked_directories(selected)
        event.accept()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.set_all_checked(True)
            event.accept()
            return
        super().keyPressEvent(event)
