"""Real dock changes, single-column touch and screen-coordinate resizing."""
import os
from pathlib import Path
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QStyle, QStyleFactory, QStyleOptionSlider, QToolTip, QWidget

from livegallery.catalog_model import CatalogModel
from livegallery.filmstrip_panel import FilmstripPanel
from livegallery.models import MediaItem
from livegallery.theme import stylesheet_for


class FilmstripPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.setProperty('liveviewerTheme', 'dark')
        self.window.setStyleSheet(stylesheet_for('dark'))
        self.layout = QGridLayout(self.window)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.photo = QWidget()
        self.model = CatalogModel(self.window)
        items = [MediaItem(str(row), Path('/synthetic') / f'Photo-{row:03d}.jpg') for row in range(80)]
        self.model.set_items(items)
        for row, item in enumerate(items):
            image = QImage(90 if row % 2 else 160, 120 if row % 2 else 90, QImage.Format.Format_RGB32)
            image.fill('#b89662' if row % 2 else '#497e9a')
            self.model.put_thumbnail(item.image_path, image)
        self.panel = FilmstripPanel(self.model)
        self.panel.layoutRequested.connect(self.dock)
        self.panel.thicknessRequested.connect(self.resize_panel)
        self.preferences = {'horizontal': 200, 'vertical': 220}
        self.requests = []
        self.window.resize(1000, 700)
        self.dock('horizontal', 'right')
        self.window.show()
        self.window.activateWindow()
        self.settle()
        self.device = QTest.createTouchDevice()
        self.clicked = QSignalSpy(self.panel.strip.clicked)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def settle(self):
        for _ in range(4):
            self.app.processEvents()
            QTest.qWait(1)

    def dock(self, orientation, side):
        anchor = self.panel.capture_anchor()
        self.layout.removeWidget(self.panel)
        self.layout.removeWidget(self.photo)
        for number in (0, 1):
            self.layout.setRowStretch(number, 0)
            self.layout.setColumnStretch(number, 0)
        self.panel.set_mode(orientation, side)
        if orientation == 'horizontal':
            self.layout.addWidget(self.photo, 0, 0)
            self.layout.addWidget(self.panel, 1, 0)
            self.layout.setRowStretch(0, 1)
            self.layout.setColumnStretch(0, 1)
        else:
            photo_column = 1 if side == 'left' else 0
            self.layout.addWidget(self.photo, 0, photo_column)
            self.layout.addWidget(self.panel, 0, 1 - photo_column)
            self.layout.setRowStretch(0, 1)
            self.layout.setColumnStretch(photo_column, 1)
        self.panel.set_extent(self.preferences[orientation])
        self.panel.restore_anchor(anchor)

    def resize_panel(self, orientation, requested):
        self.requests.append((orientation, requested))
        self.preferences[orientation] = max(160 if orientation == 'horizontal' else 180, min(480, requested))
        self.panel.set_extent(self.preferences[orientation])

    def mouse_global(self, handle, kind, position, *, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
        local = handle.mapFromGlobal(position)
        event = QMouseEvent(kind, QPointF(local), QPointF(local), QPointF(position), button, buttons,
                            Qt.KeyboardModifier.NoModifier, Qt.MouseEventSource.MouseEventNotSynthesized)
        QApplication.sendEvent(handle, event)
        self.settle()

    def test_actual_buttons_jump_to_explicit_sides_and_close_without_losing_controls(self):
        original_buttons = (self.panel.hide_button, self.panel.side_button, self.panel.orientation_button)
        self.assertEqual([button.text() for button in (self.panel.side_button, self.panel.hide_button,
                                                      self.panel.orientation_button)], ['左侧', 'X', '右侧'])
        self.assertTrue(self.panel.side_button.isVisible())
        QTest.mouseClick(self.panel.orientation_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertEqual((self.panel.orientation, self.panel.side), ('vertical', 'right'))
        self.assertEqual(self.panel.hide_button.text(), 'X')
        self.assertEqual(self.panel.resize_handle.edge, 'left')
        self.assertGreater(self.panel.x(), self.photo.x())
        QTest.mouseClick(self.panel.side_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertEqual((self.panel.orientation, self.panel.side), ('vertical', 'left'))
        self.assertEqual(self.panel.hide_button.text(), 'X')
        self.assertEqual(self.panel.resize_handle.edge, 'right')
        self.assertLess(self.panel.x(), self.photo.x())
        QTest.mouseClick(self.panel.orientation_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertEqual(self.panel.orientation, 'horizontal')
        QTest.mouseClick(self.panel.orientation_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertEqual(self.panel.side, 'right')
        QTest.mouseClick(self.panel.orientation_button, Qt.MouseButton.LeftButton)
        self.settle()
        QTest.mouseClick(self.panel.side_button, Qt.MouseButton.LeftButton)
        self.settle()
        self.assertEqual((self.panel.orientation, self.panel.side), ('vertical', 'left'))
        hidden = QSignalSpy(self.panel.hideRequested)
        QTest.mouseClick(self.panel.hide_button, Qt.MouseButton.LeftButton)
        self.assertEqual(hidden.count(), 1)
        self.assertEqual((self.panel.hide_button, self.panel.side_button, self.panel.orientation_button), original_buttons)
        self.assertIs(self.panel.controls.parentWidget(), self.panel._body)

    def test_bottom_controls_stay_in_fixed_right_column_without_covering_or_scrolling_with_photos(self):
        panel = self.panel

        def bounds(widget):
            return QRect(widget.mapTo(panel, QPoint()), widget.size())

        for theme in ('dark', 'light'):
            self.window.setStyleSheet(stylesheet_for(theme))
            for width, height in ((520, 160), (1000, 200)):
                with self.subTest(theme=theme, width=width, height=height):
                    self.window.resize(width, 700)
                    self.dock('horizontal', 'right')
                    panel.set_extent(height)
                    self.settle()
                    self.assertIs(panel.controls.parentWidget(), panel._body)
                    self.assertEqual(panel.controls.width(), 38)
                    buttons = (panel.hide_button, panel.side_button, panel.orientation_button)
                    self.assertEqual([button.text() for button in buttons], ['X', '左侧', '右侧'])
                    rects = [bounds(button) for button in buttons]
                    self.assertEqual(len({rect.x() for rect in rects}), 1)
                    self.assertTrue(all(first.bottom() < second.top() for first, second in zip(rects, rects[1:])))
                    for button, rect in zip(buttons, rects):
                        self.assertEqual(button.size().toTuple(), (38, 34))
                        self.assertTrue(button.isVisible())
                        self.assertTrue(panel.rect().contains(rect))
                        self.assertFalse(rect.intersects(bounds(panel.strip)))
                    self.assertGreater(bounds(panel.controls).left(), bounds(panel.strip).right())
                    self.assertEqual(panel.resize_handle.x(), panel.contentsRect().x())
                    self.assertEqual(panel.resize_handle.width(), panel.contentsRect().width())
                    self.assertEqual(bounds(panel.strip).top(), bounds(panel.resize_handle).bottom() + 1)
                    self.assertEqual(panel.findChildren(QLabel), [])
                    bar = panel.strip.horizontalScrollBar()
                    self.assertGreater(bar.maximum(), 0)
                    bar.setValue(bar.maximum())
                    self.settle()
                    self.assertEqual([bounds(button) for button in buttons], rects)
                    bar.setValue(0)
                    self.settle()
                    self.assertEqual([bounds(button) for button in buttons], rects)

    def test_mode_and_resize_preserve_model_pixmap_identity_selection_and_browsing_anchor(self):
        strip = self.panel.strip
        strip.setCurrentIndex(self.model.index(60, 0))
        strip.current_scrollbar().setValue(25 * strip.itemDelegate().sizeHint(None, None).width())
        self.settle()
        anchor = self.panel.capture_anchor()
        selected = strip.currentIndex()
        thumbnails = {path: pixmap.cacheKey() for path, pixmap in self.model.thumbnails.items()}
        resets = QSignalSpy(self.model.modelReset)
        requested = QSignalSpy(self.model.thumbnail_requested)
        for orientation, side, extent in (('vertical', 'right', 220), ('vertical', 'left', 280),
                                           ('horizontal', 'left', 260), ('horizontal', 'left', 190)):
            self.dock(orientation, side)
            self.panel.set_extent(extent)
            self.settle()
            self.assertIs(strip.model(), self.model)
            self.assertEqual(strip.currentIndex(), selected)
            self.assertEqual(self.panel.capture_anchor()['index'].row(), anchor['index'].row())
            self.assertEqual({path: pixmap.cacheKey() for path, pixmap in self.model.thumbnails.items()}, thumbnails)
        self.assertEqual(resets.count(), 0)
        self.assertEqual(requested.count(), 0)
        self.assertEqual(self.clicked.count(), 0)

    def test_vertical_single_column_native_window_touch_swipes_and_tap(self):
        self.dock('vertical', 'right')
        self.settle()
        strip = self.panel.strip
        self.assertGreater(strip.verticalScrollBar().maximum(), 1000)
        self.assertEqual(strip.horizontalScrollBar().maximum(), 0)
        first, second = (strip.visualRect(self.model.index(row, 0)) for row in (0, 1))
        self.assertEqual(first.left(), second.left())
        self.assertGreaterEqual(second.top(), first.bottom())
        strip.setCurrentIndex(self.model.index(0, 0))
        target = self.window.windowHandle()
        point = strip.viewport().rect().center()
        start = strip.viewport().mapTo(self.window, point)
        sequence = QTest.touchEvent(target, self.device, False)
        sequence.press(0, start, target).commit()
        sequence.move(0, start - QPoint(0, 80), target).commit()
        sequence.release(0, start - QPoint(0, 140), target).commit()
        self.settle()
        self.assertGreater(strip.verticalScrollBar().value(), 0)
        self.assertEqual(strip.currentIndex().row(), 0)
        self.assertEqual(self.clicked.count(), 0)
        row = strip.indexAt(point).row()
        tap = QTest.touchEvent(target, self.device, False)
        tap.press(0, start, target).commit()
        tap.release(0, start, target).commit()
        self.settle()
        self.assertEqual(self.clicked.count(), 1)
        self.assertEqual(strip.currentIndex().row(), row)
        before = strip.verticalScrollBar().value()
        wheel = QWheelEvent(QPointF(point), QPointF(strip.viewport().mapToGlobal(point)), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(strip.viewport(), wheel)
        self.assertGreater(strip.verticalScrollBar().value(), before)

    def test_right_and_left_boundary_mouse_drag_change_width_and_thumbnail_size(self):
        for side, direction in (('right', -1), ('left', 1)):
            self.dock('vertical', side)
            self.settle()
            handle = self.panel.resize_handle
            start = handle.mapToGlobal(handle.rect().center())
            original = self.panel.width()
            preview = self.panel.strip.itemDelegate().sizeHint(None, None)
            photo_width = self.photo.width()
            self.mouse_global(handle, QEvent.Type.MouseButtonPress, start,
                              button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            position = start + QPoint(direction * 40, 0)
            self.mouse_global(handle, QEvent.Type.MouseMove, position, buttons=Qt.MouseButton.LeftButton)
            self.assertEqual(self.panel.width(), original + 40)
            self.assertLess(self.photo.width(), photo_width)
            self.assertGreater(self.panel.strip.itemDelegate().sizeHint(None, None).width(), preview.width())
            count = len(self.requests)
            self.mouse_global(handle, QEvent.Type.MouseMove, position, buttons=Qt.MouseButton.LeftButton)
            self.assertEqual(len(self.requests), count)
            self.mouse_global(handle, QEvent.Type.MouseButtonRelease, position, button=Qt.MouseButton.LeftButton)

    def test_vertical_boundary_native_window_touch_and_layout_handoff_cancel_late_release(self):
        target = self.window.windowHandle()
        for side, direction in (('left', 1), ('right', -1)):
            self.dock('vertical', side)
            self.settle()
            handle = self.panel.resize_handle
            point = handle.mapTo(self.window, handle.rect().center())
            original = self.panel.width()
            sequence = QTest.touchEvent(target, self.device, False)
            sequence.press(0, point, target).commit()
            sequence.move(0, point + QPoint(45 * direction, 0), target).commit()
            self.settle()
            self.assertEqual(self.panel.width(), original + 45)
            sequence.release(0, point + QPoint(45 * direction, 0), target).commit()
        strip = self.panel.strip
        point = strip.viewport().mapTo(self.window, strip.viewport().rect().center())
        sequence = QTest.touchEvent(target, self.device, False)
        sequence.press(0, point, target).commit()
        self.dock('horizontal', 'left')
        self.settle()
        sequence.release(0, point, target).commit()
        self.settle()
        self.assertEqual(self.clicked.count(), 0)

    def test_physical_mouse_release_after_layout_change_does_not_click_but_new_click_works(self):
        strip = self.panel.strip
        point = strip.visualRect(self.model.index(1, 0)).center()
        QTest.mousePress(strip.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.dock('vertical', 'right')
        self.settle()
        QTest.mouseRelease(strip.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(self.clicked.count(), 0)
        index = strip.indexAt(strip.viewport().rect().center())
        QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, pos=strip.visualRect(index).center())
        self.assertEqual(self.clicked.count(), 1)

    def test_temporary_extent_and_silent_layout_restore_do_not_emit_user_preference_requests(self):
        layout = QSignalSpy(self.panel.layoutRequested)
        thickness = QSignalSpy(self.panel.thicknessRequested)
        self.dock('vertical', 'right')
        self.preferences['vertical'] = 360
        self.panel.set_extent(180)
        self.settle()
        self.assertEqual(self.preferences['vertical'], 360)
        self.panel.set_extent(self.preferences['vertical'])
        self.settle()
        self.assertEqual(self.panel.width(), 360)
        self.assertEqual(layout.count(), 0)
        self.assertEqual(thickness.count(), 0)

    def test_statistics_absent_and_theme_buttons_remain_large_and_available(self):
        for mode in ('light', 'dark'):
            self.window.setProperty('liveviewerTheme', mode)
            self.window.setStyleSheet(stylesheet_for(mode))
            for orientation, side in (('horizontal', 'right'), ('vertical', 'right'), ('vertical', 'left')):
                self.dock(orientation, side)
                self.settle()
                self.assertEqual(self.panel.findChildren(QLabel), [])
                for removed in ('_info', '_info_layout', 'library_label', 'analysis_label', 'set_info_visible'):
                    self.assertFalse(hasattr(self.panel, removed), removed)
                if orientation == 'vertical':
                    self.assertEqual(self.panel.strip.y(), self.panel.controls.geometry().bottom()
                                     + 1 + self.panel._body_layout.verticalSpacing())
                for button in (self.panel.hide_button, self.panel.orientation_button, self.panel.side_button):
                    if button.isVisible():
                        self.assertGreaterEqual(button.width(), 38)
                        self.assertGreaterEqual(button.height(), 30)
                        self.assertTrue(button.isEnabled())
                self.panel.hide()
                self.panel.show()
                self.settle()
                self.assertEqual((self.panel.orientation, self.panel.side), (orientation, side))

    def test_top_overlay_inset_reserves_buttons_without_changing_dimensions_or_preferences(self):
        self.dock('vertical', 'left')
        requests = QSignalSpy(self.panel.thicknessRequested)
        width = self.panel.width()
        self.panel.set_top_inset(64)
        self.settle()
        for button in (self.panel.hide_button, self.panel.orientation_button, self.panel.side_button):
            self.assertGreaterEqual(button.mapTo(self.panel, QPoint()).y(), 64)
        self.assertEqual(self.panel.width(), width)
        self.assertEqual(requests.count(), 0)
        self.panel.set_top_inset(0)
        self.settle()
        self.assertLess(self.panel.hide_button.mapTo(self.panel, QPoint()).y(), 20)

    def test_compact_font_repolish_and_hide_show_keep_exact_touch_button_geometry(self):
        original_style = self.app.style().objectName()
        styles = list(dict.fromkeys((original_style, 'Windows', 'Fusion')))
        try:
            for style in styles:
                self.assertIsNotNone(QStyleFactory.create(style))
                self.app.setStyle(style)
                for theme in ('light', 'dark'):
                    self.window.setStyleSheet(stylesheet_for(theme) + '\nQWidget {font-family:Arial;}')
                    for orientation, side in (('horizontal', 'right'), ('vertical', 'right'), ('vertical', 'left')):
                        self.dock(orientation, side)
                        self.panel.hide()
                        self.panel.show()
                        for button in (self.panel.hide_button, self.panel.orientation_button, self.panel.side_button):
                            button.style().unpolish(button)
                            button.style().polish(button)
                            button.ensurePolished()
                        self.settle()
                        for button in (self.panel.hide_button, self.panel.orientation_button, self.panel.side_button):
                            if button.isVisible():
                                self.assertEqual(button.size().toTuple(), (38, 34),
                                                 (style, theme, orientation, side, button.text(),
                                                  button.minimumSize().toTuple(), button.sizeHint().toTuple()))
        finally:
            self.app.setStyle(original_style)

    def test_right_dock_button_order_and_real_tooltip_are_complete_in_both_themes(self):
        for theme in ('dark', 'light'):
            self.window.setStyleSheet(stylesheet_for(theme))
            for orientation, side in (('vertical', 'right'), ('vertical', 'left'), ('horizontal', 'left')):
                self.dock(orientation, side)
                self.settle()
                order = ((self.panel.hide_button, self.panel.side_button, self.panel.orientation_button)
                         if orientation == 'horizontal' else
                         (self.panel.side_button, self.panel.orientation_button, self.panel.hide_button)
                         if side == 'right' else
                         (self.panel.hide_button, self.panel.orientation_button, self.panel.side_button))
                positions = [(button.mapTo(self.panel, QPoint()).y() if orientation == 'horizontal'
                              else button.mapTo(self.panel, QPoint()).x())
                             for button in order if button.isVisible()]
                self.assertEqual(positions, sorted(positions))
                self.assertTrue(all(first < second for first, second in zip(positions, positions[1:])))
            button = self.panel.orientation_button
            QToolTip.showText(button.mapToGlobal(QPoint(0, button.height())), button.toolTip(), button)
            QTest.qWait(30)
            try:
                tooltip = next(widget for widget in self.app.topLevelWidgets()
                               if widget.metaObject().className() == 'QTipLabel' and widget.isVisible())
                self.assertEqual(tooltip.text(), button.toolTip())
                self.assertGreater(tooltip.width(), button.width())
                self.assertGreaterEqual(tooltip.contentsRect().width(), tooltip.fontMetrics().horizontalAdvance(tooltip.text()))
            finally:
                QToolTip.hideText()

    def test_left_vertical_scrollbar_is_outermost_without_mirroring_thumbnails_or_reversing_touch(self):
        image = QImage(160, 90, QImage.Format.Format_RGB32)
        image.fill('#ec3030')
        painter = QPainter(image)
        painter.fillRect(80, 0, 80, 90, '#3030ec')
        painter.end()
        self.model.put_thumbnail(self.model.items[0].image_path, image)
        selected = self.panel.strip.currentIndex()
        for side in ('left', 'right'):
            self.dock('vertical', side)
            self.settle()
            strip = self.panel.strip
            bar = strip.verticalScrollBar()
            viewport = strip.viewport()
            self.assertTrue(bar.isVisible())
            bar_x = bar.mapTo(strip, QPoint()).x()
            if side == 'left':
                self.assertLess(bar_x, viewport.x())
            else:
                self.assertGreater(bar_x, viewport.x() + viewport.width() - 1)
            bar.setValue(0)
            self.settle()
            tile = strip.visualRect(self.model.index(0))
            rendered = viewport.grab().toImage()
            y = tile.center().y() - 15
            left = rendered.pixelColor(tile.center().x() - 35, y)
            right = rendered.pixelColor(tile.center().x() + 35, y)
            self.assertGreater(left.red(), left.blue() + 100)
            self.assertGreater(right.blue(), right.red() + 100)
            self.assertLess(tile.center().y(), strip.visualRect(self.model.index(1)).center().y())
            option = QStyleOptionSlider()
            option.initFrom(bar)
            option.orientation = Qt.Orientation.Vertical
            option.minimum, option.maximum, option.pageStep = bar.minimum(), bar.maximum(), bar.pageStep()
            option.sliderPosition = option.sliderValue = bar.value()
            knob = bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar, option,
                                              QStyle.SubControl.SC_ScrollBarSlider, bar)
            QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=knob.center())
            QTest.mouseMove(bar, knob.center() + QPoint(0, 60))
            QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=knob.center() + QPoint(0, 60))
            self.assertGreater(bar.value(), 0)
            bar.setValue(0)
            self.settle()
            target = self.window.windowHandle()
            point = viewport.mapTo(self.window, viewport.rect().center())
            sequence = QTest.touchEvent(target, self.device, False)
            sequence.press(0, point + QPoint(0, 60), target).commit()
            sequence.move(0, point, target).commit()
            sequence.release(0, point - QPoint(0, 60), target).commit()
            self.settle()
            self.assertGreater(bar.value(), 0)
            self.assertEqual(self.clicked.count(), 0)
            self.assertEqual(strip.currentIndex(), selected)
        self.dock('horizontal', 'right')
        self.settle()
        self.assertEqual(self.panel.strip.layoutDirection(), Qt.LayoutDirection.LeftToRight)


if __name__ == '__main__':
    unittest.main()
