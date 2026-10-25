"""Window-routed contact lifecycle regressions; physical devices are not used."""
import os
from pathlib import Path
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from livegallery.selftest import _check_pointer_contact_lifecycle, _check_pointer_input_capability
from livegallery.viewer import ViewerCanvas


class PointerContactIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_window_touch_and_legacy_mouse_survive_between_contact_leave(self):
        result = _check_pointer_contact_lifecycle(Path(__file__).parent / 'fixtures')
        self.assertTrue(result['touch_double_tap_fit_twice_fit_after_leave'])
        self.assertTrue(result['unknown_mouse_legacy_second_pause_then_drag_after_leave'])
        self.assertTrue(result['promoted_mouse_deduplicated_without_cancelling_touch'])
        self.assertEqual(result['leave_events_between_contacts'], 4)
        self.assertEqual(result['leave_receiver'], 'ViewerCanvas')
        self.assertEqual(result['hold_requests'], 2)
        self.assertFalse(result['physical_touch_hardware'])
        self.assertFalse(result['physical_touchpad_hardware'])

    def test_native_capability_report_does_not_claim_hardware_validation(self):
        viewer = ViewerCanvas()
        viewer.resize(800, 600)
        viewer.show()
        QCoreApplication.processEvents()
        try:
            result = _check_pointer_input_capability(viewer)
            self.assertTrue(result['uncorrelated_input_remains_unknown'])
            self.assertFalse(result['physical_button_state_verified'])
            self.assertFalse(result['physical_touchpad_hardware'])
            self.assertFalse(result['diagnostics']['preview_touchpad_registration_enabled'])
        finally:
            viewer.close()
            viewer.deleteLater()
            QCoreApplication.processEvents()


if __name__ == '__main__':
    unittest.main()
