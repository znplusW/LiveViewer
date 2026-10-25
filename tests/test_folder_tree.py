"""Real sidebar checkbox, range and batched keyboard interactions."""
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem, QTreeWidgetItemIterator

from livegallery.folder_tree import FolderTree
from livegallery.theme import DARK_STYLE, stylesheet_for, theme_tokens


class FolderTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'photos'
        self.a, self.b, self.c = (self.root / name for name in ('a', 'b', 'c'))
        self.child = self.b / 'child'
        self.counts = {self.root: 1, self.a: 2, self.b: 3, self.child: 4, self.c: 5}
        self.tree = FolderTree()
        self.tree.setStyleSheet(DARK_STYLE)
        self.tree.resize(500, 420)
        self.tree.set_directories((self.root, self.b), self.counts)
        self.tree.show()
        self.tree.activateWindow()
        self.app.processEvents()

    def tearDown(self):
        self.tree.close()
        self.tree.deleteLater()
        self.app.processEvents()

    def node(self, path):
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value() is not None:
            item = iterator.value()
            if item.data(0, Qt.ItemDataRole.UserRole) == str(path):
                return item
            iterator += 1
        self.fail('No folder node: ' + str(path))

    def click(self, path, *, checkbox=False, modifiers=Qt.KeyboardModifier.NoModifier):
        item = self.node(path)
        rect = self.tree.visualItemRect(item)
        if checkbox:
            option = QStyleOptionViewItem()
            self.tree.itemDelegate().initStyleOption(option, self.tree.indexFromItem(item))
            option.rect = rect
            position = self.tree.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                                        option, self.tree.viewport()).center()
        else:
            position = QPoint(rect.left() + 140, rect.center().y())
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, position)
        self.app.processEvents()

    def test_import_defaults_all_and_nested_roots_have_one_node_with_direct_counts(self):
        self.assertEqual(self.tree.known_directories, set(self.counts))
        self.assertEqual(self.tree.checked_directories, set(self.counts))
        self.assertIsNone(self.tree.selection_spec)
        self.assertIn('15', self.tree.all_item.text(0))
        self.assertIs(self.node(self.b).parent(), self.node(self.root))
        self.assertIn('3', self.node(self.b).text(0))
        self.assertNotIn('7', self.node(self.b).text(0))

    def test_row_is_single_selection_checkbox_and_control_click_toggle_independently(self):
        changed = QSignalSpy(self.tree.checkedChanged)
        self.click(self.a)
        self.assertEqual(self.tree.checked_directories, {self.a})
        self.click(self.b, checkbox=True)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b})
        self.assertNotIn(self.child, self.tree.checked_directories)
        self.click(self.b, checkbox=True)
        self.assertEqual(self.tree.checked_directories, {self.a})
        self.click(self.c, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.c})
        self.assertEqual(changed.count(), 4)
        self.assertEqual(self.tree.all_item.checkState(0), Qt.CheckState.PartiallyChecked)

    def test_shift_selects_exact_visible_range_and_ctrl_a_also_selects_collapsed_children(self):
        self.node(self.b).setExpanded(False)
        self.click(self.a)
        changed = QSignalSpy(self.tree.checkedChanged)
        self.click(self.c, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b, self.c})
        self.assertNotIn(self.child, self.tree.checked_directories)
        self.assertEqual(changed.count(), 1)
        self.tree.setFocus()
        QTest.keyClick(self.tree, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.tree.checked_directories, set(self.counts))
        self.assertEqual(changed.count(), 2)
        self.assertIsNone(changed.at(1)[0])
        self.assertEqual(self.tree.all_item.checkState(0), Qt.CheckState.Checked)

    def test_aggregate_clear_and_keyboard_space_do_not_cascade_to_children(self):
        changed = QSignalSpy(self.tree.checkedChanged)
        rect = self.tree.visualItemRect(self.tree.all_item)
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(rect.left() + 120, rect.center().y()))
        self.assertEqual(self.tree.checked_directories, set())
        self.assertEqual(changed.count(), 1)
        self.tree.setCurrentItem(self.node(self.b))
        self.tree.setFocus()
        QTest.keyClick(self.tree, Qt.Key.Key_Space)
        self.assertEqual(self.tree.checked_directories, {self.b})
        self.assertEqual(changed.count(), 2)
        self.assertNotIn(self.child, self.tree.checked_directories)

    def test_three_thousand_directory_select_all_emits_once_and_restores_subset(self):
        counts = {self.root / f'folder-{number:04d}': 1 for number in range(3000)}
        self.tree.set_directories((self.root,), counts, ())
        self.tree.setFocus()
        changed = QSignalSpy(self.tree.checkedChanged)
        QTest.keyClick(self.tree, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(self.tree.checked_directories), 3001)
        self.assertEqual(changed.count(), 1)
        selected = {self.root / 'folder-0001', self.root / 'folder-0999'}
        self.tree.set_directories((self.root,), counts, selected)
        self.assertEqual(self.tree.checked_directories, selected)
        self.assertEqual(changed.count(), 1)

    def test_hollow_unchecked_boxes_and_white_ticks_are_clear_in_both_themes_and_dpr(self):
        for theme in ('light', 'dark'):
            self.tree.setProperty('liveviewerTheme', theme)
            self.tree.setStyleSheet(stylesheet_for(theme))
            self.tree.set_checked_directories((self.a,))
            self.app.processEvents()
            colors = theme_tokens(theme)
            for ratio in (1, 2):
                viewport = self.tree.viewport()
                image = QImage(viewport.width() * ratio, viewport.height() * ratio, QImage.Format.Format_ARGB32)
                image.setDevicePixelRatio(ratio)
                viewport.render(image)
                for path in (self.a, self.b):
                    item = self.node(path)
                    option = QStyleOptionViewItem()
                    self.tree.itemDelegate().initStyleOption(option, self.tree.indexFromItem(item))
                    option.rect = self.tree.visualItemRect(item)
                    rect = self.tree.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                                            option, viewport)
                    self.assertGreaterEqual(rect.width(), 12)
                    if path == self.b:
                        self.assertEqual(image.pixelColor(rect.center() * ratio).name(), colors['field'])
                    else:
                        whites = 0
                        for y in range(rect.top() * ratio, (rect.bottom() + 1) * ratio):
                            for x in range(rect.left() * ratio, (rect.right() + 1) * ratio):
                                pixel = image.pixelColor(x, y)
                                whites += min(pixel.red(), pixel.green(), pixel.blue()) > 235
                        self.assertGreaterEqual(whites, 5 * ratio * ratio)
            self.click(self.b, checkbox=True)
            self.assertEqual(self.tree.checked_directories, {self.a, self.b})


if __name__ == '__main__':
    unittest.main()
