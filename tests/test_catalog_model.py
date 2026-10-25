"""Render the shared overview/filmstrip delegate at actual logical sizes."""
import os
from pathlib import Path
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem, QWidget

from livegallery.catalog_model import (CatalogModel, ThumbnailDelegate,
                                       _fitted_image_rect, _live_badge_layout)
from livegallery.models import MediaItem
from livegallery.theme import theme_tokens


class ThumbnailBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = QWidget()
        self.model = CatalogModel(self.widget)
        path = Path('/synthetic/Live.jpg')
        self.model.set_items([MediaItem('live', path, path.with_suffix('.MOV')),
                              MediaItem('static', path)])
        self.set_image(320, 180)

    def tearDown(self):
        self.widget.deleteLater()
        self.app.processEvents()

    def set_image(self, width, height, source_dpr=1):
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.setDevicePixelRatio(source_dpr)
        image.fill('#5794b6')
        self.model.put_thumbnail(self.model.items[0].image_path, image)

    def render(self, delegate, row=0, *, theme='dark', dpr=1, selected=False):
        self.widget.setProperty('liveviewerTheme', theme)
        option = QStyleOptionViewItem()
        option.initFrom(self.widget)
        option.widget = self.widget
        option.rect = QRect(0, 0, *delegate.sizeHint(option, self.model.index(row)).toTuple())
        option.state = QStyle.StateFlag.State_Enabled
        if selected:
            option.state |= QStyle.StateFlag.State_Selected
        image = QImage(option.rect.width() * dpr, option.rect.height() * dpr,
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(theme_tokens(theme)['surface'])
        painter = QPainter(image)
        delegate.paint(painter, option, self.model.index(row))
        painter.end()
        area = option.rect.adjusted(10, 10, -10, -34)
        pixmap = self.model.thumbnails[str(self.model.items[0].image_path)]
        photo = _fitted_image_rect(area, pixmap)
        return image, photo, _live_badge_layout(photo)

    @staticmethod
    def changed_pixels(live, still):
        return [(x, y) for y in range(live.height()) for x in range(live.width())
                if live.pixel(x, y) != still.pixel(x, y)]

    def test_small_overview_and_filmstrip_render_dot_and_growing_restores_label(self):
        for large in (True, False):
            delegate = ThumbnailDelegate(self.widget, large=large)
            for small, large_value in ((120, 260),) if large else ((64, 180),):
                setter = delegate.set_thumbnail_width if large else delegate.set_thumbnail_height
                setter(small)
                compact, photo, (mode, badge) = self.render(delegate)
                still, _, _ = self.render(delegate, 1)
                self.assertEqual(mode, 'dot')
                self.assertTrue(photo.contains(badge))
                changed = self.changed_pixels(compact, still)
                self.assertGreater(len(changed), 15)
                self.assertLessEqual(len(changed), 100)
                self.assertTrue(all(badge.contains(x, y) for x, y in changed))
                setter(large_value)
                full, photo, (mode, badge) = self.render(delegate)
                still, _, _ = self.render(delegate, 1)
                self.assertEqual(mode, 'full')
                self.assertEqual(badge.size().toTuple(), (47, 20))
                self.assertTrue(photo.contains(badge))
                self.assertGreater(len(self.changed_pixels(full, still)), 500)
                setter(small)
                self.assertEqual(self.render(delegate)[2][0], 'dot')

    def test_narrow_photo_uses_actual_photo_bounds_and_dot_stays_inside(self):
        delegate = ThumbnailDelegate(self.widget, large=True)
        delegate.set_thumbnail_width(260)
        for dimensions in ((60, 600), (1200, 45), (320, 180)):
            self.set_image(*dimensions)
            _, photo, (mode, badge) = self.render(delegate)
            self.assertTrue(photo.contains(badge))
            self.assertEqual(mode, 'full' if dimensions == (320, 180) else 'dot')
            self.assertGreaterEqual(badge.left(), photo.left())
        self.assertEqual(_live_badge_layout(QRect(0, 0, 1, 1))[1], QRect(0, 0, 1, 1))
        self.assertTrue(_live_badge_layout(QRect())[1].isEmpty())

    def test_theme_selection_and_output_dpr_preserve_visible_dot_and_logical_size(self):
        delegate = ThumbnailDelegate(self.widget, large=True)
        delegate.set_thumbnail_width(120)
        for theme in ('dark', 'light'):
            for selected in (False, True):
                for dpr in (1, 2):
                    live, _, (mode, badge) = self.render(delegate, theme=theme, dpr=dpr, selected=selected)
                    still, _, _ = self.render(delegate, 1, theme=theme, dpr=dpr, selected=selected)
                    self.assertEqual(mode, 'dot')
                    self.assertEqual(badge.size().toTuple(), (10, 10))
                    self.assertEqual(live.pixelColor(badge.center().x() * dpr, badge.center().y() * dpr),
                                     QColor(theme_tokens(theme)['badge_text']))
                    changed = self.changed_pixels(live, still)
                    self.assertGreater(len(changed), 15 * dpr * dpr)
                    self.assertTrue(all(badge.contains(x // dpr, y // dpr) for x, y in changed))

    def test_source_dpr_and_many_repaints_do_not_reload_or_reset_cached_images(self):
        delegate = ThumbnailDelegate(self.widget, large=True)
        self.set_image(320, 180, source_dpr=2)
        pixmap = self.model.thumbnails[str(self.model.items[0].image_path)]
        self.assertIsInstance(pixmap, QPixmap)
        key = pixmap.cacheKey()
        requests = QSignalSpy(self.model.thumbnail_requested)
        resets = QSignalSpy(self.model.modelReset)
        for width in (120, 204, 480, 120) * 5:
            delegate.set_thumbnail_width(width)
            _, photo, (mode, _) = self.render(delegate, dpr=2)
            self.assertGreater(photo.width(), photo.height())
            self.assertEqual(mode, 'dot' if width == 120 else 'full')
        self.assertEqual(pixmap.cacheKey(), key)
        self.assertIs(self.model.thumbnails[str(self.model.items[0].image_path)], pixmap)
        self.assertEqual(requests.count(), 0)
        self.assertEqual(resets.count(), 0)


if __name__ == '__main__':
    unittest.main()
