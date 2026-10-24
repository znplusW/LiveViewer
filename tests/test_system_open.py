"""Exercise the COM ABI against owned fake vtables; never launch an app."""
import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from livegallery import system_open as native


class FakeCom:
    def __init__(self, slots=None):
        self.releases = 0
        self.callbacks = []
        self.table = (ctypes.c_void_p * 10)()
        self.instance = ctypes.c_void_p(ctypes.addressof(self.table))
        self.address = ctypes.addressof(self.instance)
        self.install(2, [], self.release, ctypes.c_uint32)
        for index, (types, callback) in (slots or {}).items():
            self.install(index, types, callback)

    def install(self, index, types, callback, result=ctypes.c_int32):
        function = native._WINCALL(result, ctypes.c_void_p, *types)(callback)
        self.callbacks.append(function)
        self.table[index] = ctypes.cast(function, ctypes.c_void_p).value

    def release(self, _self):
        self.releases += 1
        return 0


def put_pointer(output, address):
    ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = address


class ShellFixture:
    def __init__(self, specifications, fail_next_at=None):
        self.buffers = {}
        self.freed = []
        self.handlers = []
        self.invoked = []
        self.created_paths = []
        self.bound_guids = []
        self.invoke_result = 0
        self.bind_result = 0
        self.next_index = 0
        self.fail_next_at = fail_next_at
        self.shell = MagicMock()
        self.ole = MagicMock()
        self.ole.CoTaskMemFree.side_effect = self.free
        self.shell.SHAssocEnumHandlers.side_effect = self.enumerate
        self.shell.SHCreateItemFromParsingName.side_effect = self.create
        for identity, display in specifications:
            handler = FakeCom()
            handler.install(3, [ctypes.POINTER(ctypes.c_void_p)], self.string_getter(identity))
            handler.install(4, [ctypes.POINTER(ctypes.c_void_p)], self.string_getter(display))
            handler.install(8, [ctypes.c_void_p],
                            lambda this, data: self.invoke(this, data))
            self.handlers.append(handler)
        self.enumerator = FakeCom({3: ([ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(ctypes.c_uint32)], self.next)})
        self.data = FakeCom()
        self.item = FakeCom({3: ([ctypes.c_void_p, ctypes.POINTER(native.GUID),
                                 ctypes.POINTER(native.GUID), ctypes.POINTER(ctypes.c_void_p)], self.bind)})

    def string_getter(self, text):
        def get(_self, output):
            if text is None:
                return ctypes.c_int32(0x80004005).value
            buffer = ctypes.create_unicode_buffer(text)
            address = ctypes.addressof(buffer)
            self.buffers[address] = buffer
            put_pointer(output, address)
            return 0
        return get

    def free(self, pointer):
        address = pointer.value if isinstance(pointer, ctypes.c_void_p) else pointer
        self.freed.append(address)
        self.buffers.pop(address)

    def enumerate(self, extension, flags, output):
        self.extension, self.flags = extension, flags
        put_pointer(output, self.enumerator.address)
        return 0

    def next(self, _self, count, output, fetched):
        if self.next_index == self.fail_next_at:
            return ctypes.c_int32(0x80004005).value
        available = self.next_index < len(self.handlers)
        fetched[0] = 1 if available else 0
        if available:
            put_pointer(output, self.handlers[self.next_index].address)
            self.next_index += 1
            return 0
        return 1

    def create(self, path, bind_context, iid, output):
        self.created_paths.append(path)
        self.created_iid = ctypes.string_at(iid, 16)
        put_pointer(output, self.item.address)
        return 0

    def bind(self, _self, context, handler_id, iid, output):
        self.bound_guids.append((ctypes.string_at(handler_id, 16), ctypes.string_at(iid, 16)))
        put_pointer(output, self.data.address)
        return self.bind_result

    def invoke(self, handler, data):
        self.invoked.append((handler, data))
        return self.invoke_result


class AssociationTests(unittest.TestCase):
    def enumerate(self, fixture):
        with patch.object(native, "_load_shell32", return_value=fixture.shell), \
                patch.object(native, "_load_ole32", return_value=fixture.ole):
            return native.enumerate_apps(Path("中文 空格.HEIC"))

    def test_extension_filter_unicode_desktop_packaged_dedup_and_release(self):
        fixture = ShellFixture([
            (r"C:\Apps\图片.exe", "图片查看器"),
            (r"c:/apps/图片.EXE", "相同应用的其他名称"),
            ("Microsoft.Windows.Photos_8wekyb3d8bbwe!App", "Microsoft 照片"),
            (r"C:\Portable\LiveViewer.exe", "LiveViewer"),
            ("Broken.App", None),
        ])
        applications = self.enumerate(fixture)
        try:
            self.assertEqual(fixture.extension, ".heic")
            # The exact native filter must exclude generic wildcard handlers
            # that Windows offers even for unknown, unsupported extensions.
            self.assertEqual(fixture.flags, 1)
            self.assertEqual(fixture.flags, native.ASSOC_FILTER_RECOMMENDED)
            self.assertEqual({application.display_name for application in applications},
                             {"图片查看器", "Microsoft 照片"})
            self.assertEqual(fixture.enumerator.releases, 1)
            self.assertEqual([handler.releases for handler in fixture.handlers], [0, 1, 0, 1, 1])
            self.assertEqual(fixture.buffers, {})
            self.assertEqual(len(fixture.freed), 9)
        finally:
            for application in applications:
                application.close()
                application.close()
        self.assertEqual([handler.releases for handler in fixture.handlers], [1, 1, 1, 1, 1])

    def test_invoke_passes_single_unicode_shell_data_object_without_executable_commands(self):
        fixture = ShellFixture([("Packaged.App!Photo", "照片")])
        application = self.enumerate(fixture)[0]
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "中文 空格 'quoted' & literal.HEIC"
                target.touch()
                self.assertTrue(application.invoke(target))
                self.assertEqual(fixture.created_paths, [str(target.resolve())])
                self.assertEqual(fixture.created_iid, bytes(native.IID_ISHELLITEM))
                self.assertEqual(fixture.bound_guids, [(bytes(native.BHID_DATAOBJECT), bytes(native.IID_IDATAOBJECT))])
                self.assertEqual(fixture.invoked, [(fixture.handlers[0].address, fixture.data.address)])
                self.assertEqual((fixture.item.releases, fixture.data.releases), (1, 1))
                fixture.invoke_result = ctypes.c_int32(native.HRESULT_CANCELLED).value
                self.assertFalse(application.invoke(target))
                fixture.invoke_result = ctypes.c_int32(0x80004005).value
                with self.assertRaisesRegex(OSError, "80004005"):
                    application.invoke(target)
                fixture.bind_result = ctypes.c_int32(0x80004005).value
                with self.assertRaisesRegex(OSError, "80004005"):
                    application.invoke(target)
                self.assertEqual((fixture.item.releases, fixture.data.releases), (4, 4))
                with self.assertRaises(FileNotFoundError):
                    application.invoke(target.with_name("missing.heic"))
        finally:
            application.close()

    def test_partial_enumeration_failure_releases_retained_handlers_and_enumerator(self):
        fixture = ShellFixture([("First.App", "第一项"), ("Second.App", "第二项")], fail_next_at=1)
        with self.assertRaisesRegex(OSError, "80004005"):
            self.enumerate(fixture)
        self.assertEqual(fixture.enumerator.releases, 1)
        self.assertEqual(fixture.handlers[0].releases, 1)
        self.assertEqual(fixture.buffers, {})

    def test_empty_enumeration_and_pointer_abi(self):
        fixture = ShellFixture([])
        self.assertEqual(self.enumerate(fixture), [])
        self.assertEqual(fixture.enumerator.releases, 1)
        self.assertEqual(ctypes.sizeof(native.GUID), 16)
        self.assertEqual(ctypes.sizeof(native._HRESULT), 4)
        self.assertEqual(ctypes.sizeof(native._ULONG), 4)
        self.assertEqual(native._ComPtr(ctypes.c_void_p()).address, 0)


if __name__ == "__main__":
    unittest.main()
