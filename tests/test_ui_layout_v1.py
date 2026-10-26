"""User-visible layout states for the agreed v1.0 desktop changes."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMenu, QPushButton, QStatusBar

from livegallery.app import MainWindow, SettingsDialog
from livegallery.settings import Preferences
from livegallery.ui_theme import application_theme


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'LIVEGALLERY_DATA_DIR': str(self.base / 'data')})
        self.environment.start()
        self.window = MainWindow()
        self.window.resize(940, 650)
        self.window.show()
        self.application.processEvents()

    def tearDown(self):
        self.window.close()
        self.assertTrue(self.window.wait_for_shutdown(5), self.window._shutdown_error)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.processEvents()
        self.environment.stop()
        self.temporary.cleanup()

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            QTest.qWait(10)
        self.fail('Timed out waiting for the requested visible layout state')

    def load_picture(self):
        folder = self.base / 'photos'
        folder.mkdir()
        image = QImage(640, 480, QImage.Format.Format_RGB32)
        image.fill('#406078')
        self.assertTrue(image.save(str(folder / '正常照片.PNG')))
        self.window.load_folder(folder)
        self.wait_for(self.window._current_image_available)
        self.window.toast.hide()
        self.application.processEvents()

    @staticmethod
    def button_background(button):
        image = button.grab().toImage()
        scale = image.devicePixelRatio()
        return image.pixelColor(round(8 * scale), round(button.height() * scale / 2)).name()

    def assert_sidebar_statistics(self):
        window = self.window
        labels = (window.library_label, window.analysis_label, window.hint_label)
        for label in labels:
            self.assertTrue(window.sidebar.isAncestorOf(label))
            self.assertFalse(window.film.isAncestorOf(label))
            self.assertTrue(label.wordWrap())
            self.assertEqual(label.isVisible(), window.sidebar.isVisible())
        if window.sidebar.isVisible():
            bounds = [QRect(label.mapTo(window.sidebar, QPoint()), label.size()) for label in labels]
            for rectangle in bounds:
                self.assertTrue(window.sidebar.rect().contains(rectangle))
            for first, second in zip(bounds, bounds[1:]):
                self.assertLess(first.bottom(), second.top())
            tree_bottom = window.tree.mapTo(window.sidebar, window.tree.rect().bottomLeft()).y()
            self.assertLess(tree_bottom, bounds[0].top())

    def test_compact_toolbar_keeps_touch_targets_and_add_emphasis_tracks_library(self):
        window = self.window
        self.assertEqual(window.brand_label.text(), 'LiveViewer')
        self.assertTrue(window.welcome_tagline.isVisible())
        self.assertNotIn('让照片中的瞬间，再动起来', [label.text() for label in window.header.findChildren(QLabel)])
        self.wait_for(lambda: window.empty_body.height() >= window.empty_body.heightForWidth(window.empty_body.width()))
        self.assertTrue(window.empty_body.isVisible())
        self.assertTrue(window.empty_open.isVisible())
        window.resize(1420, 930)
        self.wait_for(lambda: window.empty_icon.isVisible() and window.empty_icon.height() >= window.empty_icon.sizeHint().height())
        self.assertGreaterEqual(window.empty_body.height(), window.empty_body.heightForWidth(window.empty_body.width()))
        window.resize(940, 650)
        self.wait_for(lambda: window.empty_body.height() >= window.empty_body.heightForWidth(window.empty_body.width()))
        empty_background = self.button_background(window.open_button)
        self.load_picture()
        self.assertNotEqual(self.button_background(window.open_button), empty_background)
        for theme in ('dark', 'light'):
            application_theme(theme)
            self.application.processEvents()
            self.assertLessEqual(window.header.height(), 64)
            for button in window.header.findChildren(QPushButton):
                if button.isVisible():
                    self.assertGreaterEqual(button.height(), 38, button.text())
                    self.assertTrue(window.header.rect().contains(button.geometry()), button.text())
        self.assertFalse(window.welcome_tagline.isVisible())
        self.assertEqual(window.open_button.text(), '＋  添加文件夹')
        loaded_background = self.button_background(window.open_button)
        window.search.setText('no-matching-filename')
        self.application.processEvents()
        self.assertEqual(window.model.rowCount(), 0)
        self.assertEqual(self.button_background(window.open_button), loaded_background)
        window._remove_folders(window.folders)
        self.wait_for(lambda: not window._items and not window._scan_pending)
        self.assertEqual(window.open_button.objectName(), 'primary')

    def test_overview_removes_single_photo_row_without_hiding_filters_or_sidebar(self):
        self.load_picture()
        window = self.window
        window.grid_button.click()
        self.application.processEvents()
        self.assertIs(window.content.currentWidget(), window.grid)
        self.assertFalse(window.infobar.isVisible())
        self.assertTrue(window.filterbar.isVisible())
        self.assertTrue(window.sidebar.isVisible())
        self.assertEqual(window.grid_button.text(), '◧  单张')
        titles = []
        menu = QMenu(window)
        def inspect(_position):
            titles.extend(action.text() for action in menu.actions())
        with patch('livegallery.app.QMenu', return_value=menu), \
                patch.object(menu, 'exec', side_effect=inspect) as execute:
            window._item_menu(window.grid.mapToGlobal(window.grid.rect().center()))
            execute.assert_called_once()
        self.assertIn('独立预览窗', titles)
        self.assertIn('文件与配对详情', titles)  # Existing operation menu stays intact.
        self.assertNotIn('文件信息', titles)
        window.sidebar_button.click()
        window.grid_button.click()
        self.application.processEvents()
        self.assertTrue(window.infobar.isVisible())
        self.assertTrue(window.filterbar.isVisible())
        self.assertFalse(window.sidebar.isVisible())
        self.assertEqual(window.grid_button.text(), '▦  全览')
        window.grid_button.click()
        self.application.processEvents()
        self.assertFalse(window.sidebar.isVisible())
        window._toggle_fullscreen()
        window._set_chrome(True)
        self.assertFalse(window.header.isVisible())
        self.assertFalse(window.infobar.isVisible())
        window._exit_fullscreen()
        self.assertTrue(window.header.isVisible())
        self.assertFalse(window.infobar.isVisible())

    def test_detached_preview_actions_remain_reachable_without_overview_row(self):
        self.load_picture()
        window = self.window
        window._detach_preview()
        self.application.processEvents()
        self.assertTrue(window._detached)
        self.assertFalse(window.infobar.isVisible())
        self.assertEqual(window.detach_button.text(), '返回主窗口')
        self.assertEqual(window.map_button.text(), '触控板')
        for button in (window.detach_button, window.map_button):
            self.assertIs(button.parentWidget(), window.header)
            self.assertTrue(button.isVisible())
            self.assertTrue(window.header.rect().contains(button.geometry()), button.text())
        window.map_button.click()
        self.assertTrue(window._map_visible)
        window.detach_button.click()
        self.application.processEvents()
        self.assertFalse(window._detached)
        self.assertFalse(window._map_visible)
        self.assertTrue(window.infobar.isVisible())
        self.assertIs(window.detach_button.parentWidget(), window.infobar)
        self.assertFalse(window.map_button.isVisible())
        self.assertEqual(window.detach_button.text(), '独立预览窗')

    def test_photo_information_row_and_film_owned_right_controls_do_not_overlap(self):
        self.load_picture()
        window = self.window
        window._toggle_strip(True)
        window._set_film_layout('horizontal', 'right')
        window.live_badge.setText('! LIVE')
        window.live_badge.show()  # Exercise the most crowded actual information row.
        splitter = window.sidebar.parentWidget()

        def bounds(widget):
            return QRect(widget.mapTo(window, QPoint()), widget.size())

        for theme, width, sidebar_width in (('dark', 940, 220), ('light', 940, 370),
                                             ('dark', 1420, 370), ('light', 1420, 0)):
            application_theme(theme)
            window.resize(width, 800)
            window.sidebar_button.setChecked(bool(sidebar_width))
            window._toggle_sidebar(bool(sidebar_width))
            if sidebar_width:
                splitter.setSizes([sidebar_width, width - sidebar_width])
            self.wait_for(lambda: window.infobar.isVisible() and bool(window.resolution_label.text())
                          and all(bounds(window.photo_title).contains(bounds(label))
                                  for label in (window.filename_label, window.resolution_label)))
            self.application.processEvents()
            self.assertTrue(window.live_badge.isVisible())
            self.assertEqual(window.findChildren(QStatusBar), [])
            self.assertIs(window.filename_label.parentWidget(), window.photo_title)
            self.assertIs(window.resolution_label.parentWidget(), window.photo_title)
            self.assertLess(bounds(window.filename_label).bottom(), bounds(window.resolution_label).top())
            self.assertGreaterEqual(window.filename_label.width(), 96)
            for label in (window.filename_label, window.resolution_label):
                self.assertGreaterEqual(label.height(), label.fontMetrics().height())
                self.assertTrue(bounds(window.photo_title).contains(bounds(label)),
                                {'theme': theme, 'requested_width': width, 'sidebar_width': sidebar_width,
                                 'label': label.objectName(), 'label_bounds': bounds(label).getRect(),
                                 'title_bounds': bounds(window.photo_title).getRect(),
                                 'info_bounds': bounds(window.infobar).getRect(),
                                 'info_minimum': window.infobar.minimumSize().toTuple(),
                                 'info_hint': window.infobar.minimumSizeHint().toTuple(),
                                 'row_minimum': window.infobar.row.minimumSize().toTuple(),
                                 'title_hint': window.photo_title.minimumSizeHint().toTuple()})
            row = (window.photo_title, window.sound_button, window.zoom_controls, window.fit_button,
                   window.live_badge, window.detach_button, window.counter_label, window.more_button)
            visible = [widget for widget in row if widget.isVisible()]
            for widget in visible:
                self.assertTrue(bounds(window.infobar).contains(bounds(widget)), type(widget).__name__)
            for first, second in zip(visible, visible[1:]):
                self.assertLess(bounds(first).right(), bounds(second).left())
            self.assertEqual(window.sound_button.text(), '')
            for button in (window.sound_button, window.zoom_out_button, window.zoom_in_button,
                           window.fit_button, window.more_button):
                self.assertGreaterEqual(button.height(), 38)
                self.assertGreaterEqual(button.width(), 38)
            self.assertTrue(window.infobar.isAncestorOf(window.sound_button))
            self.assertTrue(window.infobar.isAncestorOf(window.fit_button))
            self.assertIs(window.film.controls.parentWidget(), window.film._body)
            buttons = (window.film.hide_button, window.film.side_button, window.film.orientation_button)
            self.assertEqual([button.text() for button in buttons], ['X', '左侧', '右侧'])
            self.assertEqual(len({bounds(button).left() for button in buttons}), 1)
            for first, second in zip(buttons, buttons[1:]):
                self.assertLess(bounds(first).bottom(), bounds(second).top())
            for button in buttons:
                self.assertEqual(button.size().toTuple(), (38, 34))
                self.assertTrue(bounds(window.film.controls).contains(bounds(button)))
            self.assertTrue(bounds(window.film._body).contains(bounds(window.film.controls)))
            self.assertLess(bounds(window.strip).right(), bounds(window.film.controls).left())
            self.assert_sidebar_statistics()
            self.assertEqual(bounds(window.strip).top(), bounds(window.film.resize_handle).bottom() + 1)
            self.assertLessEqual(abs(bounds(window.photo_area).bottom() + 1 - bounds(window.film.resize_handle).top()), 1)

    def test_dock_controls_keep_actions_and_fullscreen_overview_visibility(self):
        self.load_picture()
        window = self.window
        window._toggle_strip(True)
        window._set_film_layout('horizontal', 'right')
        controls = window.film.controls
        buttons = (window.film.side_button, window.film.hide_button, window.film.orientation_button)
        window.film.orientation_button.click()
        self.wait_for(lambda: window.film.orientation == 'vertical' and controls.parentWidget() is window.film._body)
        self.assertEqual(window.film.side, 'right')
        window.film.side_button.click()
        self.assertEqual(window.film.side, 'left')
        window.film.orientation_button.click()
        self.wait_for(lambda: controls.parentWidget() is window.film._body)
        self.assertEqual(window.film.orientation, 'horizontal')
        self.assertEqual(buttons, (window.film.side_button, window.film.hide_button, window.film.orientation_button))
        window._toggle_fullscreen()
        self.application.processEvents()
        self.assertFalse(window.infobar.isVisible())
        self.assertIs(controls.parentWidget(), window.film._body)
        self.assertTrue(controls.isVisible())
        window.film.hide_button.click()
        self.assertFalse(window.film.isVisible())
        self.assertFalse(controls.isVisible())
        window._toggle_strip_shortcut()
        self.application.processEvents()
        self.assertTrue(controls.isVisible())
        self.assertFalse(window.infobar.isVisible())
        window._exit_fullscreen()
        self.wait_for(lambda: controls.parentWidget() is window.film._body)
        window.grid_button.click()
        self.application.processEvents()
        self.assertFalse(controls.isVisible())
        self.assertFalse(window.infobar.isVisible())
        window.grid_button.click()
        self.wait_for(lambda: controls.parentWidget() is window.film._body and controls.isVisible())
        window._detach_preview()
        self.application.processEvents()
        self.assertFalse(controls.isVisible())
        window.detach_button.click()
        self.wait_for(lambda: controls.parentWidget() is window.film._body and controls.isVisible())

    def test_short_play_hint_and_statistics_stay_in_sidebar_across_views(self):
        self.load_picture()
        window = self.window
        window.resize(1420, 930)
        window._toggle_strip(True)
        window._set_film_layout('horizontal', 'right')
        window.preferences.live_key = 'Space'
        window._update_hint()
        self.wait_for(lambda: window.hint_label.text() == '按住鼠标/触屏/Space播放')
        self.assertTrue(window.sidebar.isAncestorOf(window.hint_label))
        original = (window.library_label.text(), window.analysis_label.text())
        for theme in ('dark', 'light'):
            application_theme(theme)
            self.application.processEvents()
            self.assert_sidebar_statistics()
            self.assertEqual(window.library_label.font().pixelSize(), 10)
            self.assertEqual(window.analysis_label.font().pixelSize(), 10)
            self.assertEqual((window.library_label.text(), window.analysis_label.text()), original)
            self.assertEqual(window.film.resize_handle.height(), 14)
        window.preferences.live_key = 'Ctrl+G'
        window._update_hint()
        self.wait_for(lambda: window.hint_label.text() == '按住鼠标/触屏/Ctrl+G播放')
        self.assert_sidebar_statistics()
        labels = (window.library_label, window.analysis_label, window.hint_label)
        positions = [label.mapTo(window.sidebar, QPoint()) for label in labels]
        for orientation, side in (('vertical', 'left'), ('vertical', 'right'), ('horizontal', 'right')):
            window._set_film_layout(orientation, side)
            self.application.processEvents()
            self.assert_sidebar_statistics()
            self.assertEqual([label.mapTo(window.sidebar, QPoint()) for label in labels], positions)
            self.assertEqual(window.library_label.font().pixelSize(), 10)
        window.grid_button.click()
        self.application.processEvents()
        self.assertIs(window.content.currentWidget(), window.grid)
        self.assertFalse(window.film.isVisible())
        self.assert_sidebar_statistics()
        self.assertEqual([label.mapTo(window.sidebar, QPoint()) for label in labels], positions)
        window.sidebar_button.click()
        self.application.processEvents()
        self.assertFalse(window.sidebar.isVisible())
        self.assert_sidebar_statistics()
        window.grid_button.click()
        self.application.processEvents()
        self.assert_sidebar_statistics()
        window.sidebar_button.click()
        self.application.processEvents()
        self.assertTrue(window.sidebar.isVisible())
        self.assert_sidebar_statistics()

    def test_zoom_preference_hides_all_three_controls_and_persists(self):
        self.load_picture()
        window = self.window
        self.assertTrue(window.preferences.show_zoom_controls)
        widgets = (window.zoom_out_button, window.zoom_label, window.zoom_in_button)
        for visible in (False, True):
            dialog = SettingsDialog(window.preferences, window)
            dialog.show_zoom_controls.setChecked(visible)
            try:
                with patch('livegallery.app.SettingsDialog', return_value=dialog), \
                        patch.object(dialog, 'exec', return_value=QDialog.DialogCode.Accepted):
                    window.open_settings()
                self.wait_for(lambda: all(widget.isVisible() == visible for widget in widgets))
                self.assertEqual(window.zoom_controls.isVisible(), visible)
                self.assertEqual(Preferences.load().show_zoom_controls, visible)
                self.assertTrue(window.sound_button.isVisible())
                self.assertTrue(window.fit_button.isVisible())
                self.assertTrue(window.more_button.isVisible())
            finally:
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_narrow_preview_entry_moves_to_menu_and_returns_when_wide(self):
        self.load_picture()
        window = self.window
        window.resize(940, 800)
        window.sidebar.parentWidget().setSizes([370, 570])
        self.wait_for(lambda: not window.detach_button.isVisible())
        self.assertTrue(window.more_button.isVisible() and window.more_button.isEnabled())
        menu = QMenu(window)
        selected = []

        def choose_preview(_position):
            actions = [action for action in menu.actions() if action.text() == '独立预览窗']
            self.assertEqual(len(actions), 1)
            self.assertTrue(actions[0].isEnabled())
            selected.append(actions[0].text())
            actions[0].trigger()

        with patch('livegallery.app.QMenu', return_value=menu), \
                patch.object(menu, 'exec', side_effect=choose_preview):
            window._show_item_menu()
        self.assertEqual(selected, ['独立预览窗'])
        self.wait_for(lambda: window._detached)
        self.assertTrue(window.detach_button.isVisible())
        self.assertIs(window.detach_button.parentWidget(), window.header)
        window.detach_button.click()
        self.wait_for(lambda: not window._detached)
        window.resize(1420, 930)
        self.wait_for(lambda: window.detach_button.isVisible())
        self.assertIs(window.detach_button.parentWidget(), window.infobar)
        self.assertTrue(window.infobar.rect().contains(window.detach_button.geometry()))
        self.assertGreaterEqual(window.filename_label.width(), 96)

    def test_rescan_after_synthetic_photo_removed_clears_stale_resolution(self):
        self.load_picture()
        window = self.window
        self.assertTrue(window.resolution_label.text())
        self.assertEqual(window.library_label.text(), '共 1 项 · 0 个 LIVE')
        photo = self.base / 'photos' / '正常照片.PNG'
        self.assertTrue(photo.is_relative_to(self.base))
        photo.unlink()  # Only this test's generated temporary image.
        window._rescan()
        self.wait_for(lambda: not window._scan_pending and window.model.rowCount() == 0)
        self.assertEqual(window.resolution_label.text(), '')
        self.assertIsNone(window._displayed_item)
        self.assertEqual(window.library_label.text(), '共 0 项 · 0 个 LIVE')
        self.assertTrue(window.analysis_label.text().startswith('待分析 0 项 · '))

        # Exercise the actual scanner and public filter signals with a known
        # synthetic pair, rather than assigning the model or count labels.
        image = QImage(640, 480, QImage.Format.Format_RGB32)
        image.fill('#507088')
        for name in ('static-filter.PNG', 'live-filter.JPG'):
            self.assertTrue(image.save(str(photo.parent / name)))
        shutil.copyfile(Path(__file__).parent / 'fixtures' / 'Live_H264.MOV',
                        photo.parent / 'live-filter.MOV')
        window._rescan()
        self.wait_for(lambda: not window._scan_pending and window.model.rowCount() == 2)
        self.assertEqual(window.library_label.text(), '共 2 项 · 1 个 LIVE')
        window.kind_filter.static.click()
        self.wait_for(lambda: window.model.rowCount() == 1)
        self.assertEqual(window.library_label.text(), '共 1 项 · 1 个 LIVE')
        window.kind_filter.static.click()
        window.kind_filter.live.click()
        self.wait_for(lambda: window.model.rowCount() == 1)
        self.assertEqual(window.library_label.text(), '共 1 项 · 0 个 LIVE')
        window.kind_filter.live.click()
        window.search.setText('live-filter')
        self.wait_for(lambda: window.model.rowCount() == 1)
        self.assertEqual(window.library_label.text(), '共 1 项 · 1 个 LIVE')
        window.search.setText('not-present-in-this-synthetic-folder')
        self.wait_for(lambda: window.model.rowCount() == 0)
        self.assertEqual(window.library_label.text(), '共 0 项 · 0 个 LIVE')
        window.search.clear()
        self.wait_for(lambda: window.model.rowCount() == 2)
        self.assertEqual(window.library_label.text(), '共 2 项 · 1 个 LIVE')
        window._remove_folders(tuple(window.folders))
        self.wait_for(lambda: not window._items and not window._scan_pending)
        self.assertEqual(window.library_label.text(), '共 0 项 · 0 个 LIVE')
        self.assertTrue(window.analysis_label.text().startswith('待分析 0 项 · '))
        self.assert_sidebar_statistics()


if __name__ == '__main__':
    unittest.main()
