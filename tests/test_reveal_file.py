"""Unicode PIDL selection and COM ownership without opening real windows."""
import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from livegallery import reveal_file as native


class RevealTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / '旅行,图片 01.jpg'
        self.path.write_bytes(b'owned test file')
        self.shell, self.ole = MagicMock(), MagicMock()
        self.ole.CoInitializeEx.return_value = 0
        self.shell.SHParseDisplayName.side_effect = self.parse
        self.shell.SHOpenFolderAndSelectItems.return_value = 0
        self.parsed = []

    def parse(self, name, context, output, flags, attributes):
        self.parsed.append((name, context, flags, attributes))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x123456
        return 0

    def invoke(self):
        with patch.object(native, '_load_native', return_value=(self.shell, self.ole)):
            return native.reveal_in_folder(self.path)

    def test_full_unicode_item_pidl_selects_file_without_command_parsing(self):
        self.assertTrue(self.invoke())
        self.assertEqual(self.parsed, [(str(self.path.resolve()), None, 0, None)])
        arguments = self.shell.SHOpenFolderAndSelectItems.call_args.args
        self.assertEqual(arguments[0].value, 0x123456)
        self.assertEqual(arguments[1:], (0, None, 0))
        self.assertEqual(self.ole.CoTaskMemFree.call_args.args[0].value, 0x123456)
        self.ole.CoUninitialize.assert_called_once_with()
        self.ole.CoInitializeEx.assert_called_once_with(None, 2)

    def test_existing_sta_initialization_is_balanced(self):
        self.ole.CoInitializeEx.return_value = 1  # S_FALSE also owns one init.
        self.invoke()
        self.ole.CoUninitialize.assert_called_once_with()

    def test_existing_other_apartment_is_not_uninitialized(self):
        self.ole.CoInitializeEx.return_value = ctypes.c_int32(0x80010106).value
        self.invoke()
        self.ole.CoUninitialize.assert_not_called()
        self.ole.CoTaskMemFree.assert_called_once()

    def test_initialization_failure_never_parses_or_uninitializes(self):
        self.ole.CoInitializeEx.return_value = ctypes.c_int32(0x80004005).value
        with self.assertRaisesRegex(OSError, '初始化'):
            self.invoke()
        self.shell.SHParseDisplayName.assert_not_called()
        self.ole.CoUninitialize.assert_not_called()

    def test_selection_failure_frees_pidl_and_uninitializes(self):
        self.shell.SHOpenFolderAndSelectItems.return_value = ctypes.c_int32(0x80004005).value
        with self.assertRaisesRegex(OSError, '选中'):
            self.invoke()
        self.ole.CoTaskMemFree.assert_called_once()
        self.ole.CoUninitialize.assert_called_once()

    def test_parse_failure_frees_any_returned_pidl_and_uninitializes(self):
        def failed(*args):
            self.parse(*args)
            return ctypes.c_int32(0x80004005).value
        self.shell.SHParseDisplayName.side_effect = failed
        with self.assertRaisesRegex(OSError, '读取'):
            self.invoke()
        self.shell.SHOpenFolderAndSelectItems.assert_not_called()
        self.ole.CoTaskMemFree.assert_called_once()
        self.ole.CoUninitialize.assert_called_once()

    def test_empty_pidl_is_rejected_with_balanced_com(self):
        self.shell.SHParseDisplayName.side_effect = lambda *args: 0
        with self.assertRaisesRegex(OSError, '未返回'):
            self.invoke()
        self.shell.SHOpenFolderAndSelectItems.assert_not_called()
        self.ole.CoTaskMemFree.assert_not_called()
        self.ole.CoUninitialize.assert_called_once()

    def test_missing_file_is_reported_before_native_calls(self):
        self.path.unlink()
        with patch.object(native, '_load_native') as load:
            with self.assertRaises(FileNotFoundError):
                native.reveal_in_folder(self.path)
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
