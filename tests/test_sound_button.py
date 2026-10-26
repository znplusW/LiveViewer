"""Offscreen speaker geometry, palette painting and native button behavior."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QAccessible, QColor, QImage, QMouseEvent, QPalette
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QPushButton

from livegallery.sound_button import SoundButton
from livegallery.theme import theme_palette


class SoundButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.button = SoundButton()
        self.button.resize(self.button.sizeHint())
        self.button.show()
        self.application.processEvents()

    def tearDown(self):
        self.button.close()
        self.button.deleteLater()
        self.application.processEvents()

    def click(self):
        # Deliver local Qt events without moving the desktop cursor.
        point = QPointF(self.button.rect().center())
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            event = QMouseEvent(kind, point, self.button.mapToGlobal(point), Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(self.button, event)

    def render(self, ratio=1):
        image = QImage(self.button.width()*ratio, self.button.height()*ratio, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        self.button.render(image)
        return image

    def ink_count(self, image, color):
        ratio = image.devicePixelRatio()
        total = 0
        for y in range(round(7*ratio), round(31*ratio)):
            for x in range(round(7*ratio), round(31*ratio)):
                pixel = image.pixelColor(x, y)
                if max(abs(pixel.red()-color.red()), abs(pixel.green()-color.green()),
                       abs(pixel.blue()-color.blue())) <= 16:
                    total += 1
        return total

    def test_default_is_textless_muted_touch_target_with_action_and_accessible_state(self):
        button = self.button
        self.assertEqual(button.text(), '')
        self.assertTrue(button.isCheckable())
        self.assertTrue(button.isChecked())
        self.assertGreaterEqual(button.width(), 38)
        self.assertGreaterEqual(button.height(), 38)
        self.assertGreaterEqual(button.minimumWidth(), 38)
        self.assertGreaterEqual(button.minimumHeight(), 38)
        self.assertEqual(button.toolTip(), '开启声音')
        self.assertEqual(button.accessibleName(), '开启声音')
        self.assertEqual(button.accessibleDescription(), '当前已静音')
        accessible = QAccessible.queryAccessibleInterface(button)
        self.assertTrue(accessible.state().checked)
        reference = QPushButton()
        reference.setCheckable(True)
        self.assertEqual(accessible.role(), QAccessible.queryAccessibleInterface(reference).role())
        reference.deleteLater()

    def test_programmatic_state_sync_is_idempotent_and_never_emits_clicked(self):
        changed, clicked = QSignalSpy(self.button.toggled), QSignalSpy(self.button.clicked)
        self.button.set_muted(False)
        self.button.set_muted(False)
        self.assertEqual((changed.count(), clicked.count()), (1, 0))
        self.assertFalse(self.button.isChecked())
        self.assertEqual(self.button.toolTip(), '静音')
        self.assertEqual(self.button.accessibleName(), '静音')
        self.assertEqual(self.button.accessibleDescription(), '当前声音开启')
        self.button.setChecked(True)
        self.assertEqual((changed.count(), clicked.count()), (2, 0))
        self.assertEqual(self.button.toolTip(), '开启声音')
        self.assertTrue(QAccessible.queryAccessibleInterface(self.button).state().checked)

    def test_mouse_and_space_each_toggle_once_and_disabled_state_blocks_both(self):
        clicked, toggled = QSignalSpy(self.button.clicked[bool]), QSignalSpy(self.button.toggled)
        self.click()
        self.assertFalse(self.button.isChecked())
        self.assertEqual((clicked.count(), toggled.count()), (1, 1))
        self.assertEqual(clicked.at(0), [False])
        self.button.setFocus()
        QTest.keyClick(self.button, Qt.Key.Key_Space)
        self.assertTrue(self.button.isChecked())
        self.assertEqual((clicked.count(), toggled.count()), (2, 2))
        self.assertEqual(clicked.at(1), [True])
        self.button.setEnabled(False)
        self.click()
        QTest.keyClick(self.button, Qt.Key.Key_Space)
        self.assertTrue(self.button.isChecked())
        self.assertEqual((clicked.count(), toggled.count()), (2, 2))

    def test_both_line_icons_render_in_current_theme_palette_at_one_and_two_dpr(self):
        for theme in ('dark', 'light'):
            palette = theme_palette(theme)
            self.button.setPalette(palette)
            background = '#102030'
            self.button.setStyleSheet('QPushButton { background:'+background+'; border:0px; padding:0px; }')
            expected = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText)
            for ratio in (1, 2):
                with self.subTest(theme=theme, ratio=ratio):
                    pictures = []
                    for muted in (False, True):
                        self.button.set_muted(muted)
                        image = self.render(ratio)
                        self.assertGreater(self.ink_count(image, expected), 30*ratio)
                        # The speaker body remains an outline, not a filled glyph.
                        self.assertEqual(image.pixelColor(16*ratio, 19*ratio).name(), background)
                        pictures.append(image)
                    self.assertNotEqual(pictures[0], pictures[1], 'Mute cross and sound waves rendered identically')

    def test_palette_changes_and_disabled_button_text_are_used_without_cached_icons(self):
        self.button.setStyleSheet('QPushButton { background:#102030; border:0px; padding:0px; }')
        for enabled, foreground in ((True, '#e4f137'), (True, '#49d7e8'), (False, '#af83ba')):
            with self.subTest(enabled=enabled, foreground=foreground):
                palette = QPalette(self.button.palette())
                for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
                    palette.setColor(group, QPalette.ColorRole.ButtonText, QColor(foreground))
                self.button.setPalette(palette)
                self.button.setEnabled(enabled)
                self.assertGreater(self.ink_count(self.render(), QColor(foreground)), 30)


if __name__ == '__main__':
    unittest.main()
