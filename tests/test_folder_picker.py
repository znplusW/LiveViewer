"""Actual multi-directory UI selection and owned Windows COM ABI regression."""
import ctypes
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTreeView, QWidget

from livegallery import folder_picker as picker
from livegallery.system_open import _WINCALL


class FolderPickerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.first, self.second = self.base / 'A 照片', self.base / 'B pictures'
        self.first.mkdir()
        self.second.mkdir()
        (self.base / 'keep.txt').write_text('not a folder')
        self.parent = QWidget()
        self.parent.show()
        self.app.processEvents()

    def tearDown(self):
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def choose(self, *, cancel=False):
        errors, observations = [], []
        deadline = time.monotonic() + 4

        def interact():
            dialog = QApplication.activeModalWidget()
            try:
                if not isinstance(dialog, picker._MultiFolderDialog):
                    if time.monotonic() >= deadline:
                        raise AssertionError('No multi-folder dialog appeared')
                    QTimer.singleShot(10, interact)
                    return
                view = dialog.findChild(QTreeView, 'treeView')
                model = view.model()
                indices = [model.index(str(path)) for path in (self.first, self.second)]
                rectangles = [view.visualRect(index) for index in indices]
                if not all(index.isValid() and not rect.isEmpty()
                           for index, rect in zip(indices, rectangles)):
                    if time.monotonic() >= deadline:
                        raise AssertionError('Directory rows did not become visible')
                    QTimer.singleShot(10, interact)
                    return
                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rectangles[0].center())
                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.ControlModifier, pos=rectangles[1].center())
                observations.append([Path(value) for value in dialog.selectedFiles()])
                buttons = dialog.findChild(QDialogButtonBox)
                button = buttons.button(QDialogButtonBox.StandardButton.Cancel if cancel
                                        else QDialogButtonBox.StandardButton.Open)
                assert button is not None and button.isEnabled()
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                if dialog.isVisible():
                    raise AssertionError('Actual folder confirmation button did not close the dialog')
            except Exception as error:
                errors.append(error)
                if dialog is not None:
                    dialog.reject()

        QTimer.singleShot(0, interact)
        with patch.object(picker, '_native_libraries', side_effect=OSError('test Qt fallback')):
            selected = picker.select_photo_folders(self.parent, self.base)
        if errors:
            raise errors[0]
        self.assertEqual(set(observations[0]), {self.first, self.second})
        return selected

    def test_real_ctrl_multi_selection_and_confirmation_return_both_directories(self):
        before = sorted(path.relative_to(self.base) for path in self.base.rglob('*'))
        self.assertEqual(set(self.choose()), {self.first, self.second})
        self.assertEqual(sorted(path.relative_to(self.base) for path in self.base.rglob('*')), before)
        self.assertEqual((self.base / 'keep.txt').read_text(), 'not a folder')

    def test_real_multi_selection_then_cancel_returns_empty(self):
        self.assertEqual(self.choose(cancel=True), [])

    def test_native_setup_unavailable_falls_back_to_real_multi_select_dialog(self):
        with patch.object(picker.sys, 'platform', 'win32'):
            self.assertEqual(set(self.choose()), {self.first, self.second})


class FakeCom:
    def __init__(self):
        self.releases = 0
        self.callbacks = []
        self.table = (ctypes.c_void_p * 30)()
        self.instance = ctypes.c_void_p(ctypes.addressof(self.table))
        self.address = ctypes.addressof(self.instance)
        self.install(2, [], self.release, ctypes.c_uint32)

    def install(self, index, types, callback, result=ctypes.c_int32):
        function = _WINCALL(result, ctypes.c_void_p, *types)(callback)
        self.callbacks.append(function)
        self.table[index] = ctypes.cast(function, ctypes.c_void_p).value

    def release(self, _this):
        self.releases += 1
        return 0


def put_pointer(output, address):
    ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = address


class NativeFixture:
    def __init__(self, paths, *, show_result=0, result_error=0, string_error=False):
        self.ole, self.shell = MagicMock(), MagicMock()
        self.ole.CoInitializeEx.return_value = 0
        self.ole.CoCreateInstance.side_effect = self.create
        self.ole.CoTaskMemFree.side_effect = self.free
        self.shell.SHCreateItemFromParsingName.side_effect = self.create_initial
        self.dialog, self.results, self.initial = FakeCom(), FakeCom(), FakeCom()
        self.items = []
        self.buffers, self.freed, self.seen = {}, [], {}
        self.result_error = result_error
        self.dialog.install(10, [ctypes.POINTER(ctypes.c_uint32)], self.get_options)
        self.dialog.install(9, [ctypes.c_uint32], lambda _this, flags: self.record('flags', flags))
        self.dialog.install(17, [ctypes.c_wchar_p], lambda _this, title: self.record('title', title))
        self.dialog.install(18, [ctypes.c_wchar_p], lambda _this, text: self.record('button', text))
        self.dialog.install(12, [ctypes.c_void_p], lambda _this, item: self.record('initial_item', item))
        self.dialog.install(3, [ctypes.c_void_p], lambda _this, owner: self.record('owner', owner, show_result))
        self.dialog.install(27, [ctypes.POINTER(ctypes.c_void_p)], self.get_results)
        self.results.install(7, [ctypes.POINTER(ctypes.c_uint32)], self.count)
        self.results.install(8, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)], self.item_at)
        for path in paths:
            item = FakeCom()
            item.install(5, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                         self.string_getter(str(path), string_error))
            self.items.append(item)

    def record(self, key, value, result=0):
        self.seen[key] = value
        return result

    def create(self, clsid, outer, context, iid, output):
        self.seen.update(clsid=ctypes.string_at(clsid, 16), iid=ctypes.string_at(iid, 16), context=context)
        put_pointer(output, self.dialog.address)
        return 0

    def create_initial(self, path, context, iid, output):
        self.seen['initial_path'] = path
        put_pointer(output, self.initial.address)
        return 0

    def get_options(self, _this, output):
        output[0] = 0x1000
        return 0

    def get_results(self, _this, output):
        put_pointer(output, self.results.address)
        return self.result_error

    def count(self, _this, output):
        output[0] = len(self.items)
        return 0

    def item_at(self, _this, index, output):
        put_pointer(output, self.items[index].address)
        return 0

    def string_getter(self, path, error):
        def get(_this, format_kind, output):
            self.seen['display_name_kind'] = format_kind
            buffer = ctypes.create_unicode_buffer(path)
            address = ctypes.addressof(buffer)
            self.buffers[address] = buffer
            put_pointer(output, address)
            return ctypes.c_int32(0x80004005).value if error else 0
        return get

    def free(self, pointer):
        self.freed.append(pointer.value)
        self.buffers.pop(pointer.value)


class NativeFolderPickerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.paths = [self.base / '中文 一', self.base / 'second']
        for path in self.paths:
            path.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def select(self, fixture, initial=None):
        parent = MagicMock()
        parent.window.return_value.winId.return_value = 8765
        with patch.object(picker, '_native_libraries', return_value=(fixture.ole, fixture.shell)):
            return picker._native_select_photo_folders(parent, initial)

    def test_abi_options_owner_unicode_results_initial_folder_and_balanced_resources(self):
        fixture = NativeFixture(self.paths)
        self.assertEqual(self.select(fixture, self.base), self.paths)
        self.assertEqual(fixture.seen['flags'], 0x1000 | picker.FOLDER_OPTIONS)
        self.assertEqual(fixture.seen['owner'], 8765)
        self.assertEqual(fixture.seen['initial_path'], str(self.base))
        self.assertEqual(fixture.seen['initial_item'], fixture.initial.address)
        self.assertEqual(fixture.seen['clsid'], bytes(picker.CLSID_FILE_OPEN_DIALOG))
        self.assertEqual(fixture.seen['iid'], bytes(picker.IID_IFILE_OPEN_DIALOG))
        self.assertEqual(fixture.seen['display_name_kind'], picker.SIGDN_FILESYSPATH)
        self.assertIn('Ctrl', fixture.seen['title'])
        self.assertEqual([value.releases for value in fixture.items], [1, 1])
        self.assertEqual(fixture.dialog.releases, 1)
        self.assertEqual(fixture.results.releases, 1)
        self.assertEqual(fixture.initial.releases, 1)
        self.assertEqual(len(fixture.freed), 2)
        self.assertFalse(fixture.buffers)
        fixture.ole.CoUninitialize.assert_called_once_with()

    def test_cancel_is_quiet_does_not_fetch_results_and_releases_dialog(self):
        fixture = NativeFixture(self.paths, show_result=ctypes.c_int32(picker.HRESULT_CANCELLED).value)
        self.assertEqual(self.select(fixture), [])
        self.assertEqual(fixture.dialog.releases, 1)
        self.assertEqual(fixture.results.releases, 0)
        fixture.ole.CoUninitialize.assert_called_once_with()

    def test_result_failure_after_show_releases_partial_result_without_fallback(self):
        fixture = NativeFixture(self.paths, result_error=ctypes.c_int32(0x80004005).value)
        with self.assertRaises(OSError) as caught:
            self.select(fixture)
        self.assertNotIsInstance(caught.exception, picker._NativeDialogUnavailable)
        self.assertEqual(fixture.dialog.releases, 1)
        self.assertEqual(fixture.results.releases, 1)
        fixture.ole.CoUninitialize.assert_called_once_with()

    def test_failed_display_name_also_frees_allocated_string_and_item(self):
        fixture = NativeFixture(self.paths, string_error=True)
        with self.assertRaises(OSError):
            self.select(fixture)
        self.assertEqual(fixture.items[0].releases, 1)
        self.assertEqual(fixture.results.releases, 1)
        self.assertFalse(fixture.buffers)
        self.assertEqual(len(fixture.freed), 1)

    def test_com_s_false_is_balanced_but_changed_apartment_never_uninitializes(self):
        fixture = NativeFixture(self.paths)
        fixture.ole.CoInitializeEx.return_value = 1
        self.assertEqual(self.select(fixture), self.paths)
        fixture.ole.CoUninitialize.assert_called_once_with()
        unavailable = NativeFixture(self.paths)
        unavailable.ole.CoInitializeEx.return_value = ctypes.c_int32(0x80010106).value
        with self.assertRaises(picker._NativeDialogUnavailable):
            self.select(unavailable)
        unavailable.ole.CoUninitialize.assert_not_called()
        unavailable.ole.CoCreateInstance.assert_not_called()

    def test_failed_creation_and_configuration_release_owned_partial_dialog(self):
        fixture = NativeFixture(self.paths)
        def failed_create(*args):
            fixture.create(*args)
            return ctypes.c_int32(0x80040154).value
        fixture.ole.CoCreateInstance.side_effect = failed_create
        with self.assertRaises(picker._NativeDialogUnavailable):
            self.select(fixture)
        self.assertEqual(fixture.dialog.releases, 1)
        fixture.ole.CoUninitialize.assert_called_once_with()
        configuration = NativeFixture(self.paths)
        configuration.dialog.install(9, [ctypes.c_uint32],
                                     lambda *_: ctypes.c_int32(0x80004005).value)
        with self.assertRaises(picker._NativeDialogUnavailable):
            self.select(configuration)
        self.assertEqual(configuration.dialog.releases, 1)
        self.assertNotIn('owner', configuration.seen)
        configuration.ole.CoUninitialize.assert_called_once_with()

    def test_public_native_cancel_never_opens_fallback(self):
        with patch.object(picker.sys, 'platform', 'win32'), \
                patch.object(picker, '_native_select_photo_folders', return_value=[]), \
                patch.object(picker, '_MultiFolderDialog') as fallback:
            self.assertEqual(picker.select_photo_folders(), [])
        fallback.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Real Windows COM configuration requires Windows')
    def test_real_windows_com_creation_options_and_initial_folder_without_showing_ui(self):
        seen = []
        original = picker._ComPtr
        class ConfigurationProbe(original):
            def call(this, slot, types, *args):
                if slot == 3:
                    flags = ctypes.c_uint32()
                    picker._hresult(super().call(10, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(flags)),
                                    '读取原生多选选项失败')
                    seen.append(flags.value)
                    return ctypes.c_int32(picker.HRESULT_CANCELLED).value
                return super().call(slot, types, *args)
        with patch.object(picker, '_ComPtr', ConfigurationProbe):
            self.assertEqual(picker._native_select_photo_folders(None, self.base), [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0] & picker.FOLDER_OPTIONS, picker.FOLDER_OPTIONS)


if __name__ == '__main__':
    unittest.main()
