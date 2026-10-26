"""Real sidebar checkbox, range and batched keyboard interactions."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator, QVBoxLayout, QWidget

from livegallery.folder_tree import FolderTree
from livegallery.theme import DARK_STYLE, stylesheet_for, theme_palette, theme_tokens


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
            position = self.tree.checkbox_rect(item).center()
        else:
            position = QPoint(self.tree.checkbox_rect(item).right() + 12, rect.center().y())
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, position)
        self.app.processEvents()

    def test_parent_stylesheet_queues_selection_palette_once_outside_repolish_with_latest_theme(self):
        class Parent(QWidget):
            style_depth = 0

            def setStyleSheet(self, value):
                self.style_depth += 1
                try:
                    return super().setStyleSheet(value)
                finally:
                    self.style_depth -= 1

        parent = Parent()
        QVBoxLayout(parent).addWidget(self.tree)
        self.tree.setStyleSheet('')
        parent.show()
        self.app.processEvents()
        try:
            # Application palettes use blue Highlight; the sidebar's selected
            # rows use separate theme tokens. Recreate that actual mismatch.
            with patch.object(self.tree, '_applying_selection_palette', True):
                self.tree.setPalette(theme_palette('dark'))
            writes = []
            original = self.tree.setPalette

            def set_palette(value):
                writes.append(parent.style_depth)
                return original(value)

            with patch.object(self.tree, 'setPalette', side_effect=set_palette):
                for scheme in ('dark', 'light', 'dark', 'light'):
                    parent.setProperty('liveviewerTheme', scheme)
                    parent.setStyleSheet(stylesheet_for(scheme))
                self.assertEqual(writes, [], 'Palette was mutated inside parent QSS repolish')
                self.app.processEvents()
                self.assertEqual(writes, [0], 'Repeated style notifications were not coalesced')
                colors = theme_tokens('light')
                self.assertEqual(self.tree.palette().color(QPalette.ColorRole.Highlight).name(), colors['selected'])
                self.assertEqual(self.tree.palette().color(QPalette.ColorRole.HighlightedText).name(), colors['selected_text'])
                for _ in range(4):
                    QCoreApplication.sendEvent(self.tree, QEvent(QEvent.Type.PaletteChange))
                self.app.processEvents()
                self.assertEqual(writes, [0], 'Unchanged colors caused another native palette write')
        finally:
            self.tree.setParent(None)
            parent.close()
            parent.deleteLater()
            self.app.processEvents()

    def test_pending_selection_palette_update_dies_with_tree(self):
        from shiboken6 import isValid
        errors = []
        tree = FolderTree()
        timer = tree._selection_palette_timer
        with patch('sys.excepthook', side_effect=lambda *error: errors.append(error)):
            QCoreApplication.sendEvent(tree, QEvent(QEvent.Type.StyleChange))
            self.assertTrue(timer.isActive())
            tree.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        self.assertFalse(isValid(tree))
        self.assertFalse(isValid(timer))
        self.assertEqual(errors, [])

    def test_import_defaults_all_and_nested_roots_have_one_node_with_direct_counts(self):
        self.assertEqual(self.tree.known_directories, set(self.counts))
        self.assertEqual(self.tree.checked_directories, set(self.counts))
        self.assertIsNone(self.tree.selection_spec)
        self.assertIn('15', self.tree.all_item.text(0))
        self.assertIs(self.node(self.b).parent(), self.node(self.root))
        self.assertIn('3', self.node(self.b).text(0))
        self.assertNotIn('7', self.node(self.b).text(0))

    def test_plain_and_control_name_click_only_move_anchor_while_checkboxes_toggle_independently(self):
        self.tree.set_checked_directories((self.a,))
        changed = QSignalSpy(self.tree.checkedChanged)
        self.click(self.b)
        self.assertEqual(self.tree.checked_directories, {self.a})
        self.assertIs(self.tree.currentItem(), self.node(self.b))
        self.assertEqual(self.tree._anchor, self.b)
        self.click(self.c, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.tree.checked_directories, {self.a})
        self.assertIs(self.tree.currentItem(), self.node(self.c))
        self.assertEqual(self.tree._anchor, self.c)
        self.assertEqual(changed.count(), 0)
        self.click(self.b, checkbox=True)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b})
        self.assertEqual(self.tree._anchor, self.b)
        self.assertNotIn(self.child, self.tree.checked_directories)
        self.click(self.b, checkbox=True)
        self.assertEqual(self.tree.checked_directories, {self.a})
        self.click(self.c, checkbox=True, modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.c})
        self.assertEqual(changed.count(), 3)
        self.assertEqual(self.tree.all_item.checkState(0), Qt.CheckState.PartiallyChecked)

    def test_shift_selects_exact_visible_range_and_ctrl_a_also_selects_collapsed_children(self):
        self.node(self.b).setExpanded(False)
        self.tree.set_checked_directories(())
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

    def test_shift_name_and_checkbox_ranges_keep_anchor_and_checkbox_sets_new_anchor(self):
        self.node(self.b).setExpanded(False)
        self.tree.set_checked_directories((self.root,))
        changed = QSignalSpy(self.tree.checkedChanged)
        self.click(self.a)
        self.assertEqual(self.tree.checked_directories, {self.root})
        self.assertEqual(changed.count(), 0)
        self.click(self.c, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b, self.c})
        self.assertEqual(self.tree._anchor, self.a)
        self.click(self.b, checkbox=True, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b})
        self.assertEqual(self.tree._anchor, self.a)
        self.assertNotIn(self.child, self.tree.checked_directories)
        self.click(self.b, checkbox=True)
        self.assertEqual(self.tree.checked_directories, {self.a})
        self.assertEqual(self.tree._anchor, self.b)
        self.click(self.c, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.b, self.c})
        self.click(self.a, modifiers=Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b, self.c})
        self.assertEqual(self.tree._anchor, self.b)
        self.assertEqual(changed.count(), 5)

    def test_checkbox_anchor_supports_upward_shift_range_including_visible_child(self):
        self.tree.set_checked_directories(())
        self.click(self.c, checkbox=True)
        changed = QSignalSpy(self.tree.checkedChanged)
        self.click(self.a, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b, self.child, self.c})
        self.click(self.b, checkbox=True, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.b, self.child, self.c})
        self.assertEqual(self.tree._anchor, self.c)
        self.assertEqual(changed.count(), 2)

    def test_aggregate_clear_and_keyboard_space_do_not_cascade_to_children(self):
        changed = QSignalSpy(self.tree.checkedChanged)
        rect = self.tree.visualItemRect(self.tree.all_item)
        name = QPoint(self.tree.checkbox_rect(self.tree.all_item).right() + 12, rect.center().y())
        for modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier):
            QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, name)
            self.assertEqual(self.tree.checked_directories, set(self.counts))
            self.assertEqual(changed.count(), 0)
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.tree.checkbox_rect(self.tree.all_item).center())
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

    def test_removal_context_checked_branches_include_descendants_but_unchecked_row_is_independent(self):
        self.tree.set_checked_directories((self.a, self.b))
        self.assertEqual(self.tree.removal_targets(self.node(self.b)), (self.a, self.b))
        self.assertEqual(self.tree.removal_count((self.a, self.b)), 3)
        self.assertEqual(self.tree.removal_targets(self.node(self.child)), (self.child,))
        self.assertEqual(self.tree.checked_directories, {self.a, self.b})
        self.tree.set_checked_directories((self.root, self.b))
        self.assertEqual(self.tree.removal_targets(self.node(self.b)), (self.root,))
        self.assertEqual(self.tree.removal_count((self.root,)), 5)

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
                image.fill(QColor(colors['surface']))
                viewport.render(image)
                for path in (self.a, self.b):
                    item = self.node(path)
                    rect = self.tree.checkbox_rect(item)
                    self.assertEqual(rect.size().width(), 24)
                    self.assertGreaterEqual(self.tree.visualItemRect(item).height(), 38)
                    if path == self.a:
                        # Native focus primitives previously left blue corner
                        # fragments immediately outside the painted checkbox.
                        row = self.tree.visualItemRect(item)
                        for y in range(max(0, row.top()) * ratio, (row.bottom() + 1) * ratio):
                            for x in range(0, rect.left() * ratio):
                                pixel = image.pixelColor(x, y)
                                self.assertFalse(pixel.blue() > pixel.red() + 40 and pixel.green() > pixel.red() + 20)
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

    def test_same_structure_background_refresh_keeps_visible_target_and_collapsed_state(self):
        counts = {self.root / f'folder-{row:03d}': 1 for row in range(60)}
        self.tree.set_directories((self.root,), counts, ())
        self.tree.verticalScrollBar().setValue(15)
        self.app.processEvents()
        target = self.root / 'folder-015'
        item = self.tree._nodes[target]
        point = self.tree.visualItemRect(item).center()
        scroll = self.tree.verticalScrollBar().value()
        for _ in range(5):
            self.tree.set_directories((self.root,), {path: 2 for path in counts}, ())
            self.app.processEvents()
        self.assertIs(self.tree._nodes[target], item)
        self.assertEqual(self.tree.verticalScrollBar().value(), scroll)
        self.assertIs(self.tree.itemAt(point), item)
        changed = QSignalSpy(self.tree.checkedChanged)
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.tree.checkbox_rect(item).center())
        self.assertEqual(self.tree.checked_directories, {target})
        self.assertEqual(changed.count(), 1)
        self.tree._nodes[self.root].setExpanded(False)
        self.tree.set_directories((self.root,), counts, self.tree.checked_directories)
        self.assertFalse(self.tree._nodes[self.root].isExpanded())

    def test_press_is_cancelled_by_structure_scroll_font_collapse_or_focus_and_release_never_toggles_another_row(self):
        counts = {self.root / f'folder-{row:03d}': 1 for row in range(50)}
        for route in ('rebuild', 'scroll', 'font', 'style', 'collapse', 'focus'):
            with self.subTest(route=route):
                self.tree.set_directories((self.root,), counts, ())
                self.tree._nodes[self.root].setExpanded(True)
                self.tree.verticalScrollBar().setValue(0)
                self.tree.setFocus()
                self.app.processEvents()
                target = self.root / 'folder-000'
                point = self.tree.checkbox_rect(self.tree._nodes[target]).center()
                changed = QSignalSpy(self.tree.checkedChanged)
                QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
                self.assertEqual(self.tree.checked_directories, set())
                if route == 'rebuild':
                    self.tree.set_directories((self.root,), {self.root / 'ahead': 1, **counts}, ())
                elif route == 'scroll':
                    self.tree.verticalScrollBar().setValue(4)
                elif route == 'font':
                    font = self.tree.font(); font.setPixelSize(max(12, font.pixelSize()) + 2); self.tree.setFont(font)
                elif route == 'style':
                    QApplication.sendEvent(self.tree, QEvent(QEvent.Type.StyleChange))
                    self.assertIsNone(self.tree._pointer, 'Gesture cancellation was deferred with the palette')
                    self.assertTrue(self.tree._selection_palette_timer.isActive())
                elif route == 'collapse':
                    self.tree._nodes[self.root].setExpanded(False)
                else:
                    QApplication.sendEvent(self.tree, QEvent(QEvent.Type.FocusOut))
                self.app.processEvents()
                QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
                self.assertEqual(self.tree.checked_directories, set())
                self.assertEqual(changed.count(), 0)

    def drag_points(self, first, last, *, text=False):
        row = self.tree.visualItemRect(self.node(first))
        start = (QPoint(self.tree.checkbox_rect(self.node(first)).right() + 12, row.center().y())
                 if text else QPoint(self.tree.viewport().width() - 8, row.top() + 2))
        end = QPoint(start.x() + 70 if text else start.x() - 70,
                     self.tree.visualItemRect(self.node(last)).bottom() - 2)
        return start, end

    def mouse_move(self, position, modifiers):
        # QTest.mouseMove has no modifiers argument. Send one ordinary Qt
        # mouse move with explicit held modifiers/buttons; no native injection.
        viewport = self.tree.viewport()
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(position),
                            QPointF(viewport.mapToGlobal(position)), Qt.MouseButton.NoButton,
                            Qt.MouseButton.LeftButton, modifiers)
        QCoreApplication.sendEvent(viewport, event)
        self.app.processEvents()

    def rubber_band(self, first, last, modifiers=Qt.KeyboardModifier.ControlModifier, *, text=False):
        start, end = self.drag_points(first, last, text=text)
        baseline = self.tree.checked_directories
        changed = QSignalSpy(self.tree.checkedChanged)
        QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, start)
        for fraction in (.2, .5, .8, 1):
            position = start + (end - start) * fraction
            self.mouse_move(position, modifiers)
            self.assertEqual(changed.count(), 0)
            self.assertEqual(self.tree.checked_directories, baseline)
        self.assertTrue(self.tree._rubber_band.isVisible())
        QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, end)
        self.app.processEvents()
        self.assertFalse(self.tree._rubber_band.isVisible())
        return changed.count()

    def test_control_blank_rubber_band_xor_toggles_inside_and_preserves_outside_checked_rows(self):
        self.tree.set_checked_directories(())
        self.assertEqual(self.rubber_band(self.a, self.c), 1)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b, self.child, self.c})
        self.tree.set_checked_directories((self.a, self.c))
        self.assertEqual(self.rubber_band(self.a, self.b), 1)
        self.assertEqual(self.tree.checked_directories, {self.b, self.c})
        self.assertEqual(self.rubber_band(self.a, self.b), 1)
        self.assertEqual(self.tree.checked_directories, {self.a, self.c}, 'Repeating the same frame restores its baseline')
        self.tree.set_checked_directories(self.counts)
        self.assertEqual(self.rubber_band(self.a, self.b), 1)
        self.assertEqual(self.tree.checked_directories, {self.root, self.child, self.c})
        self.assertEqual(self.rubber_band(self.a, self.b), 1)
        self.assertEqual(self.tree.checked_directories, set(self.counts))

    def test_control_name_drag_xor_toggles_range_from_press_baseline(self):
        self.tree.set_checked_directories((self.a, self.c))
        baseline = {self.a, self.c}
        start, end = self.drag_points(self.a, self.b, text=True)
        _, farther = self.drag_points(self.a, self.c, text=True)
        control = Qt.KeyboardModifier.ControlModifier
        changed = QSignalSpy(self.tree.checkedChanged)
        QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton, control, start)
        # Expansion and contraction use the press baseline. Rows crossed by
        # an earlier, larger frame must not be toggled by the final commit.
        for position in (farther, end, farther, end):
            self.mouse_move(position, control)
            self.assertTrue(self.tree._rubber_band.isVisible())
            self.assertEqual(self.tree.checked_directories, baseline)
            self.assertEqual(changed.count(), 0)
        QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton, control, end)
        self.app.processEvents()
        self.assertFalse(self.tree._rubber_band.isVisible())
        self.assertEqual(changed.count(), 1)
        self.assertEqual(self.tree.checked_directories, {self.b, self.c})
        self.assertNotIn(self.child, self.tree.checked_directories)
        self.assertEqual(self.rubber_band(self.a, self.b, text=True), 1)
        self.assertEqual(self.tree.checked_directories, baseline)

    def test_plain_or_shift_only_drag_from_name_or_blank_never_starts_selection_band(self):
        for text in (False, True):
            for modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
                with self.subTest(text=text, modifiers=modifiers):
                    self.tree.set_checked_directories((self.c,))
                    changed = QSignalSpy(self.tree.checkedChanged)
                    start, end = self.drag_points(self.a, self.b, text=text)
                    QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, start)
                    self.mouse_move(end, modifiers)
                    self.assertFalse(self.tree._rubber_band.isVisible())
                    QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, end)
                    self.assertEqual(self.tree.checked_directories, {self.c})
                    self.assertEqual(changed.count(), 0)

    def test_control_must_be_present_on_press_move_and_release_and_cannot_resume_cancelled_drag(self):
        control = Qt.KeyboardModifier.ControlModifier
        none = Qt.KeyboardModifier.NoModifier
        for text in (False, True):
            for missing in ('press', 'move', 'release', 'key_release'):
                with self.subTest(text=text, missing=missing):
                    self.tree.cancel_gesture()
                    self.tree.set_checked_directories((self.c,))
                    self.tree.setFocus()
                    changed = QSignalSpy(self.tree.checkedChanged)
                    start, end = self.drag_points(self.a, self.b, text=text)
                    QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton,
                                     none if missing == 'press' else control, start)
                    self.mouse_move(end, control)
                    self.assertEqual(self.tree._rubber_band.isVisible(), missing != 'press')
                    if missing == 'move':
                        self.mouse_move(end - QPoint(1, 1), none)
                        self.assertFalse(self.tree._rubber_band.isVisible())
                        self.mouse_move(end, control)
                    elif missing == 'key_release':
                        QTest.keyRelease(self.tree, Qt.Key.Key_Control)
                        self.assertFalse(self.tree._rubber_band.isVisible())
                        QTest.keyPress(self.tree, Qt.Key.Key_Control)
                        self.mouse_move(end, control)
                    QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton,
                                       none if missing == 'release' else control, end)
                    QTest.keyRelease(self.tree, Qt.Key.Key_Control)
                    self.assertFalse(self.tree._rubber_band.isVisible())
                    self.assertIsNone(self.tree._pointer)
                    self.assertEqual(self.tree.checked_directories, {self.c})
                    self.assertEqual(changed.count(), 0)
                    # Cancellation applies only to the old sequence.
                    self.assertEqual(self.rubber_band(self.a, self.b, text=text), 1)
                    self.assertEqual(self.tree.checked_directories, {self.a, self.b, self.c})

    def test_control_drag_does_not_start_on_checkbox_or_native_expand_arrow(self):
        self.tree.set_checked_directories((self.c,))
        item = self.node(self.b)
        rect = self.tree.visualItemRect(item)
        points = (self.tree.checkbox_rect(item).center(), QPoint(rect.left() - 8, rect.center().y()))
        for point in points:
            with self.subTest(point=point):
                changed = QSignalSpy(self.tree.checkedChanged)
                QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
                end = point + QPoint(100, 70)
                self.mouse_move(end, Qt.KeyboardModifier.ControlModifier)
                self.assertFalse(self.tree._rubber_band.isVisible())
                QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, end)
                self.assertEqual(self.tree.checked_directories, {self.c})
                self.assertEqual(changed.count(), 0)

    def test_plain_and_control_blank_click_do_not_check_or_replace_name_anchor(self):
        self.tree.set_checked_directories((self.c,))
        self.click(self.a)
        anchor = self.tree._anchor
        start, _ = self.drag_points(self.b, self.b)
        changed = QSignalSpy(self.tree.checkedChanged)
        for modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier):
            QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton, modifiers, start)
            self.assertEqual(self.tree.checked_directories, {self.c})
            self.assertEqual(self.tree._anchor, anchor)
        self.assertEqual(changed.count(), 0)

    def test_control_band_is_cancelled_by_layout_focus_and_selection_refresh(self):
        for route in ('rebuild', 'selection', 'font', 'style', 'collapse', 'focus', 'hide', 'resize'):
            with self.subTest(route=route):
                self.tree.show()
                self.tree.set_directories((self.root, self.b), self.counts, (self.c,))
                self.node(self.root).setExpanded(True)
                self.node(self.b).setExpanded(True)
                self.tree.setFocus()
                self.app.processEvents()
                start, end = self.drag_points(self.a, self.b)
                QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, start)
                self.mouse_move(end, Qt.KeyboardModifier.ControlModifier)
                self.assertTrue(self.tree._rubber_band.isVisible())
                baseline = {self.c}
                if route == 'rebuild':
                    self.tree.set_directories((self.root,), {**self.counts, self.root / 'new': 1}, baseline)
                elif route == 'selection':
                    baseline = {self.root}
                    self.tree.set_directories((self.root, self.b), self.counts, baseline)
                elif route == 'font':
                    font = self.tree.font(); font.setPixelSize(max(12, font.pixelSize()) + 1); self.tree.setFont(font)
                elif route == 'style':
                    QCoreApplication.sendEvent(self.tree, QEvent(QEvent.Type.StyleChange))
                elif route == 'collapse':
                    self.node(self.b).setExpanded(False)
                elif route == 'focus':
                    QCoreApplication.sendEvent(self.tree, QEvent(QEvent.Type.FocusOut))
                elif route == 'hide':
                    self.tree.hide()
                else:
                    self.tree.resize(self.tree.width() + 1, self.tree.height())
                self.app.processEvents()
                changed = QSignalSpy(self.tree.checkedChanged)
                self.assertFalse(self.tree._rubber_band.isVisible())
                QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, end)
                self.assertEqual(self.tree.checked_directories, baseline)
                self.assertEqual(changed.count(), 0)

    def test_count_refresh_preserves_name_anchor_but_removed_anchor_cannot_select_stale_range(self):
        self.tree.set_checked_directories((self.c,))
        self.click(self.a)
        self.tree.set_directories((self.root, self.b), {p: n + 1 for p, n in self.counts.items()}, (self.c,))
        self.assertEqual(self.tree._anchor, self.a)
        self.click(self.b, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.a, self.b})
        counts = {p: n for p, n in self.counts.items() if p != self.a}
        self.tree.set_directories((self.root, self.b), counts, ())
        self.assertIsNone(self.tree._anchor)
        self.click(self.c, checkbox=True, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.tree.checked_directories, {self.c})

    def test_list_blank_rubber_band_cancel_on_rebuild_and_independent_branch_control(self):
        self.tree.set_checked_directories(())
        start = QPoint(self.tree.viewport().width() - 6, self.tree.viewport().height() - 10)
        end = QPoint(start.x() - 70, self.tree.visualItemRect(self.node(self.a)).top() + 2)
        changed = QSignalSpy(self.tree.checkedChanged)
        QTest.mousePress(self.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, start)
        self.mouse_move(end, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.tree._rubber_band.isVisible())
        self.tree.set_directories((self.root,), {**self.counts, self.root / 'new': 1}, ())
        QTest.mouseRelease(self.tree.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, end)
        self.assertEqual(self.tree.checked_directories, set())
        self.assertEqual(changed.count(), 0)
        item = self.node(self.b)
        self.assertTrue(item.isExpanded())
        point = QPoint(self.tree.visualItemRect(item).left() - 8, self.tree.visualItemRect(item).center().y())
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertFalse(item.isExpanded())
        self.assertEqual(changed.count(), 0)

    def test_large_checkbox_edge_mouse_and_actual_window_touch_use_the_painted_target(self):
        self.tree.set_checked_directories(())
        box = self.tree.checkbox_rect(self.node(self.a))
        point = box.bottomRight() - QPoint(1, 1)
        changed = QSignalSpy(self.tree.checkedChanged)
        QTest.mouseClick(self.tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.tree.checked_directories, {self.a})
        self.assertEqual(changed.count(), 1)
        device = QTest.createTouchDevice()
        target = self.tree.windowHandle()
        position = self.tree.viewport().mapTo(self.tree, point)
        gesture = QTest.touchEvent(target, device, False)
        gesture.press(0, position, target).commit()
        gesture.release(0, position, target).commit()
        self.app.processEvents()
        self.assertEqual(self.tree.checked_directories, set())
        self.assertEqual(changed.count(), 2)


if __name__ == '__main__':
    unittest.main()
