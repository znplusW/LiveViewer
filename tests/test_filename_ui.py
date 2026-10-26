"""Filename mouse affordances use real offscreen widgets and synthetic paths."""
import os
from pathlib import Path
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QFocusEvent, QFont, QImage, QMouseEvent
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication, QListView, QStyleOptionViewItem

from livegallery.catalog_model import CatalogModel, ThumbnailDelegate
from livegallery.filename_field import FilenameField
from livegallery.models import MediaItem
from livegallery.touch_grid import OverviewGrid
from livegallery.touch_filmstrip import TouchFilmstrip


LONG_NAME = '2026-10-26_杭州西湖_家庭旅行_精选照片_' * 5 + '0032.HeIc'


class FilenameUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []
        self.old_interval = self.app.doubleClickInterval()
        self.app.setDoubleClickInterval(80)
        self.app.clipboard().setText('unchanged clipboard')

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.app.setDoubleClickInterval(self.old_interval)

    def view(self, kind=OverviewGrid, name=LONG_NAME):
        view = kind()
        self.widgets.append(view)
        model = CatalogModel(view)
        item = MediaItem('synthetic', Path('C:/synthetic/folder') / name)
        model.set_items([item])
        pixels = QImage(320, 180, QImage.Format.Format_RGB32)
        pixels.fill('#5794b6')
        model.put_thumbnail(item.image_path, pixels)
        view.setModel(model)
        delegate = ThumbnailDelegate(view, large=kind is OverviewGrid)
        view.setItemDelegate(delegate)
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setMovement(QListView.Movement.Static)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setUniformItemSizes(True)
        view.resize(460, 310)
        option = QStyleOptionViewItem()
        option.initFrom(view)
        view.setGridSize(delegate.sizeHint(option, model.index(0)) + QSize(5, 5))
        view.show()
        self.app.processEvents()
        return view, model, delegate

    def label_point(self, view):
        rect = view.visualRect(view.model().index(0)).adjusted(5, 5, -5, -5)
        return rect.adjusted(6, rect.height() - 24, -6, -4).center()

    def double_click(self, view, point):
        # A real double-click includes the first completed click. The first
        # release must not navigate away before the DblClick event can arrive.
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

    def test_elided_filename_double_click_copies_original_in_both_views(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            with self.subTest(view=kind.__name__):
                view, _, _ = self.view(kind)
                self.app.clipboard().setText('unchanged clipboard')
                self.double_click(view, self.label_point(view))
                self.assertEqual(self.app.clipboard().text(), LONG_NAME)
                self.assertNotIn('C:/synthetic', self.app.clipboard().text())
                view.hide()

    def test_elided_grid_first_click_cannot_hide_view_before_copy(self):
        view, _, _ = self.view()
        selected = []
        view.clicked.connect(lambda index: (selected.append(index.row()), view.hide()))
        self.double_click(view, self.label_point(view))
        QTest.qWait(100)
        self.assertEqual(self.app.clipboard().text(), LONG_NAME)
        self.assertEqual(selected, [])
        self.assertTrue(view.isVisible())

    def test_elided_single_click_activates_exactly_once_after_interval(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            view, _, _ = self.view(kind)
            clicked = QSignalSpy(view.clicked)
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=self.label_point(view))
            self.assertEqual(clicked.count(), 0)
            self.assertTrue(clicked.wait(1000))
            self.assertEqual(clicked.count(), 1)
            self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
            view.hide()

    def test_short_or_literal_ellipsis_filename_keeps_native_click_behavior(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            for name in ('tiny.JPG', 'a…b.JPG'):
                view, _, _ = self.view(kind, name)
                clicked = QSignalSpy(view.clicked)
                doubled = QSignalSpy(view.doubleClicked)
                self.double_click(view, self.label_point(view))
                self.assertGreaterEqual(clicked.count(), 1)
                self.assertEqual(doubled.count(), 1)
                self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
                view.hide()

    def test_photo_double_click_keeps_native_signals_and_clipboard(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            view, _, _ = self.view(kind)
            clicked, doubled = QSignalSpy(view.clicked), QSignalSpy(view.doubleClicked)
            self.double_click(view, view.visualRect(view.model().index(0)).center())
            self.assertGreaterEqual(clicked.count(), 1)
            self.assertEqual(doubled.count(), 1)
            self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
            view.hide()

    def test_text_row_padding_does_not_copy(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            view, _, _ = self.view(kind)
            rect = view.visualRect(view.model().index(0))
            self.double_click(view, QPoint(rect.left() + 6, self.label_point(view).y()))
            self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
            view.hide()

    def test_pending_filename_click_is_cancelled_by_lifecycle_or_model_change(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            for cancel in ('hide', 'focus', 'resize', 'model-reset', 'keyboard', 'font'):
                with self.subTest(view=kind.__name__, cancel=cancel):
                    view, model, _ = self.view(kind)
                    clicked = QSignalSpy(view.clicked)
                    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=self.label_point(view))
                    if cancel == 'hide': view.hide()
                    elif cancel == 'focus': QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut))
                    elif cancel == 'resize': view.resize(view.width() + 20, view.height())
                    elif cancel == 'model-reset': model.set_items([MediaItem('replacement', Path('C:/synthetic/new.JPG'))])
                    elif cancel == 'keyboard': QTest.keyClick(view, Qt.Key.Key_Right)
                    else:
                        font = QFont(view.font())
                        font.setPointSize(font.pointSize() + 2)
                        view.setFont(font)
                    QTest.qWait(110)
                    self.assertEqual(clicked.count(), 0)
                    self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
                    view.hide()

    def test_dragging_filename_does_not_commit_a_delayed_click(self):
        view, _, _ = self.view()
        clicked = QSignalSpy(view.clicked)
        start = self.label_point(view)
        end = start + QPoint(35, 0)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        QTest.qWait(110)
        self.assertEqual(clicked.count(), 0)
        self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        self.assertTrue(clicked.wait(1000))
        self.assertEqual(clicked.count(), 1)

    def test_model_reset_between_clicks_cannot_copy_or_activate_replacement(self):
        view, model, _ = self.view()
        clicked = QSignalSpy(view.clicked)
        point = self.label_point(view)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        replacement = LONG_NAME.replace('0032', '0048')
        model.set_items([MediaItem('replacement', Path('C:/synthetic/folder') / replacement)])
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.qWait(110)
        self.assertEqual(clicked.count(), 0)
        self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertTrue(clicked.wait(1000))
        self.assertEqual(clicked.count(), 1)

    def test_touch_label_tap_keeps_existing_immediate_action(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            view, _, _ = self.view(kind)
            clicked = QSignalSpy(view.clicked)
            point = self.label_point(view)
            sequence = QTest.touchEvent(view.viewport(), QTest.createTouchDevice(), False)
            sequence.press(0, point, view.viewport()).commit()
            sequence.release(0, point, view.viewport()).commit()
            self.app.processEvents()
            self.assertEqual(clicked.count(), 1)
            self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
            view.hide()

    def test_synthetic_mouse_double_click_does_not_copy(self):
        view, _, _ = self.view()
        point = QPointF(self.label_point(view))
        for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                              (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
                              (QEvent.Type.MouseButtonDblClick, Qt.MouseButton.LeftButton)):
            event = QMouseEvent(kind, point, point, point, Qt.MouseButton.LeftButton, buttons,
                                Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventSynthesizedByQt)
            QApplication.sendEvent(view.viewport(), event)
        self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')

    def test_tooltip_contains_original_filename_and_folder_without_elision(self):
        view, model, _ = self.view()
        tooltip = model.index(0).data(Qt.ItemDataRole.ToolTipRole)
        self.assertIn(LONG_NAME, tooltip)
        self.assertIn(str(model.items[0].image_path.parent), tooltip)
        self.assertNotIn('…', tooltip)

    def test_delegate_text_hit_geometry_matches_draw_bounds_without_thumbnail_reads(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            view, model, delegate = self.view(kind)
            model.thumbnails.clear()
            requested = QSignalSpy(model.thumbnail_requested)
            index = model.index(0)
            for size in (9, 17):
                font = QFont(view.font())
                font.setPointSize(size)
                view.setFont(font)
                option = QStyleOptionViewItem()
                option.initFrom(view)
                option.rect = view.visualRect(index)
                layout = delegate.filename_layout(option, index)
                self.assertTrue(layout.is_elided)
                self.assertEqual(layout.original, LONG_NAME)
                rect = option.rect.adjusted(5, 5, -5, -5)
                self.assertEqual(layout.displayed, option.fontMetrics.elidedText(
                    LONG_NAME, Qt.TextElideMode.ElideMiddle, rect.width() - 12))
                self.assertEqual(layout.draw_rect, rect.adjusted(6, rect.height()-24, -6, -4))
                self.assertTrue(layout.draw_rect.contains(layout.text_rect))
                self.assertTrue(view._filename_mouse._target(layout.text_rect.center()))
                self.assertIsNone(view._filename_mouse._target(
                    QPoint(layout.text_rect.center().x(), layout.draw_rect.bottom() + 1)))
            self.assertEqual(requested.count(), 0)
            self.assertEqual(model.pending, set())
            view.hide()

    def test_modified_filename_click_keeps_native_selection_without_copy(self):
        for kind in (OverviewGrid, TouchFilmstrip):
            view, _, _ = self.view(kind)
            clicked = QSignalSpy(view.clicked)
            point = self.label_point(view)
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.ControlModifier, point)
            self.assertEqual(clicked.count(), 1)
            QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.ControlModifier, point)
            self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
            view.hide()

    def test_filename_field_preserves_full_text_selection_refresh_and_copy_shortcuts(self):
        field = FilenameField('')
        self.widgets.append(field)
        field.resize(280, 30)
        path = Path('C:/synthetic/folder') / LONG_NAME
        field.set_file_path(path)
        field.show()
        field.setFocus()
        self.app.processEvents()
        self.assertTrue(field.isReadOnly())
        self.assertEqual(field.text(), LONG_NAME)
        self.assertEqual(field.cursorPosition(), 0)
        self.assertEqual(field.toolTip(), f'{LONG_NAME}\n{path.parent}')
        field.setSelection(len(LONG_NAME) - 22, 12)
        selected, cursor, cursor_rect = field.selectedText(), field.cursorPosition(), field.cursorRect()
        field.set_file_path(path)
        self.assertEqual(field.selectedText(), selected)
        self.assertEqual(field.cursorPosition(), cursor)
        self.assertEqual(field.cursorRect(), cursor_rect)
        QTest.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), selected)
        QTest.keyClick(field, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(field, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), LONG_NAME)
        field.set_file_path(path.with_name('new.JPG'))
        self.assertEqual(field.text(), 'new.JPG')
        self.assertFalse(field.hasSelectedText())
        self.assertEqual(field.cursorPosition(), 0)

    def test_filename_field_double_click_selects_word_and_native_menu_copies_selection(self):
        field = FilenameField('Photo sample with long words ' * 8 + '.HEIC')
        self.widgets.append(field)
        field.resize(260, 30)
        field.show()
        self.app.processEvents()
        QTest.mouseDClick(field, Qt.MouseButton.LeftButton, pos=QPoint(18, 15))
        self.assertTrue(field.hasSelectedText())
        self.assertNotEqual(field.selectedText(), field.text())
        self.assertEqual(self.app.clipboard().text(), 'unchanged clipboard')
        selected = field.selectedText()
        menu = field.createStandardContextMenu()
        try:
            action = next(action for action in menu.actions()
                          if 'copy' in action.text().casefold() or '复制' in action.text())
            self.assertTrue(action.isEnabled())
            action.trigger()
            self.assertEqual(self.app.clipboard().text(), selected)
        finally:
            menu.deleteLater()


if __name__ == '__main__':
    unittest.main()
