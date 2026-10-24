"""Native Windows Open With chooser for one file, without shell command text.

API and flags: https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shopenwithdialog
"""

from __future__ import annotations

import ctypes
import ntpath
import sys
import time
import uuid
from pathlib import Path


OAIF_EXEC = 0x00000004
OAIF_HIDE_REGISTRATION = 0x00000020
HRESULT_CANCELLED = 0x800704C7
E_ABORT = 0x80004004
ASSOC_FILTER_RECOMMENDED = 1
_WINCALL = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_HRESULT = ctypes.c_int32
_ULONG = ctypes.c_uint32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


IID_ISHELLITEM = GUID.parse("43826d1e-e718-42ee-bc55-a1e261c37bfe")
IID_IDATAOBJECT = GUID.parse("0000010e-0000-0000-c000-000000000046")
BHID_DATAOBJECT = GUID.parse("b8c0bd9f-ed24-455c-83e6-d5390c4fe8c4")


def _hresult(result, operation):
    value = int(result) & 0xFFFFFFFF
    if value & 0x80000000:
        raise OSError(f"{operation}（0x{value:08X}）。")
    return value


class _ComPtr:
    """One owned reference; release explicitly on the creating GUI thread."""

    def __init__(self, address):
        self.address = int((address.value if isinstance(address, ctypes.c_void_p) else address) or 0)

    def call(self, slot, argument_types, *arguments):
        if not self.address:
            raise OSError("应用关联对象已失效，请重新打开菜单。")
        table = ctypes.cast(self.address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = _WINCALL(_HRESULT, ctypes.c_void_p, *argument_types)(table[slot])
        return function(self.address, *arguments)

    def close(self):
        if self.address:
            address, self.address = self.address, 0
            table = ctypes.cast(address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            _WINCALL(_ULONG, ctypes.c_void_p)(table[2])(address)


def _load_ole32():
    if sys.platform != "win32":
        raise OSError("Windows 应用列表只能在 Windows 系统读取。")
    library = ctypes.WinDLL("ole32.dll", use_last_error=True, winmode=0x00000800)
    library.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    library.CoTaskMemFree.restype = None
    return library


def _handler_string(handler, slot, ole32):
    output = ctypes.c_void_p()
    try:
        result = handler.call(slot, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(output))
        _hresult(result, "无法读取应用名称")
        return ctypes.wstring_at(output.value).strip() if output.value else ""
    finally:
        if output.value:
            ole32.CoTaskMemFree(output)


def _canonical_identity(identity):
    # A handler name may be an EXE path or a packaged-app identity. Do not parse
    # it as a command, expand variables, or execute it directly.
    return ntpath.normcase(ntpath.normpath(identity.strip().strip('"')))


class AssociatedApplication:
    """A desktop or packaged app's IAssocHandler, retained for one open menu."""

    def __init__(self, display_name, identity, handler, shell32):
        self.display_name = display_name
        self.identity = identity
        self._handler = handler
        self._shell32 = shell32

    def close(self):
        self._handler.close()

    def invoke(self, path: Path) -> bool:
        """Pass one Unicode Shell data object to the registered handler."""
        target = Path(path).resolve()
        if not target.is_file():
            raise FileNotFoundError("图片文件暂时不可访问，请检查硬盘连接。")
        shell_item = ctypes.c_void_p()
        data_object = ctypes.c_void_p()
        create = self._shell32.SHCreateItemFromParsingName
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                           ctypes.POINTER(ctypes.c_void_p)]
        create.restype = _HRESULT
        item = data = None
        try:
            result = create(str(target), None, ctypes.byref(IID_ISHELLITEM), ctypes.byref(shell_item))
            item = _ComPtr(shell_item)
            _hresult(result, "无法读取所选图片")
            if not item.address:
                raise OSError("Windows 未返回所选图片对象。")
            result = item.call(3, [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(GUID),
                                   ctypes.POINTER(ctypes.c_void_p)], None,
                               ctypes.byref(BHID_DATAOBJECT), ctypes.byref(IID_IDATAOBJECT),
                               ctypes.byref(data_object))
            data = _ComPtr(data_object)
            _hresult(result, "无法准备所选图片")
            if not data.address:
                raise OSError("Windows 未返回所选图片数据。")
            result = int(self._handler.call(8, [ctypes.c_void_p], data.address)) & 0xFFFFFFFF
            if result in (HRESULT_CANCELLED, E_ABORT):
                return False
            _hresult(result, "无法使用所选应用打开图片")
            return True
        finally:
            if data is not None:
                data.close()
            if item is not None:
                item.close()


def enumerate_apps(path: Path) -> list[AssociatedApplication]:
    """Read Windows-recommended handlers for this format, with bounded iteration.

    All returned applications must be closed by the caller in a finally block.
    SHAssocEnumHandlers covers packaged and desktop apps without reading or
    reconstructing executable command lines. The recommended filter excludes
    generic wildcard applications offered for every extension. Other apps stay
    available through the native chooser. No association is changed.
    https://learn.microsoft.com/windows/win32/api/shobjidl_core/nf-shobjidl_core-shassocenumhandlers
    """
    extension = Path(path).suffix.casefold()
    if not extension:
        return []
    shell32, ole32 = _load_shell32(), _load_ole32()
    enumerate_native = shell32.SHAssocEnumHandlers
    enumerate_native.argtypes = [ctypes.c_wchar_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)]
    enumerate_native.restype = _HRESULT
    pointer = ctypes.c_void_p()
    applications = []
    enumerator = None
    try:
        result = enumerate_native(extension, ASSOC_FILTER_RECOMMENDED, ctypes.byref(pointer))
        enumerator = _ComPtr(pointer)
        _hresult(result, "无法读取 Windows 应用列表")
        if not enumerator.address:
            return []
        seen = set()
        deadline = time.monotonic() + 1.0
        for _index in range(64):
            if time.monotonic() >= deadline:
                break
            output, fetched = ctypes.c_void_p(), _ULONG()
            result = enumerator.call(3, [_ULONG, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(_ULONG)],
                                     1, ctypes.byref(output), ctypes.byref(fetched))
            handler = _ComPtr(output)
            retained = False
            try:
                _hresult(result, "无法读取 Windows 应用列表")
                if not fetched.value or not handler.address:
                    break
                try:
                    identity = _handler_string(handler, 3, ole32)
                    display_name = _handler_string(handler, 4, ole32)
                except OSError:
                    continue
                canonical = _canonical_identity(identity)
                if (not identity or not display_name or canonical in seen
                        or ntpath.basename(canonical) in ("liveviewer.exe", "liveviewer")
                        or display_name.casefold() == "liveviewer"):
                    continue
                seen.add(canonical)
                applications.append(AssociatedApplication(display_name, identity, handler, shell32))
                retained = True
            finally:
                if not retained:
                    handler.close()
        return sorted(applications, key=lambda application: application.display_name.casefold())
    except Exception:
        for application in applications:
            application.close()
        raise
    finally:
        if enumerator is not None:
            enumerator.close()


class OPENASINFO(ctypes.Structure):
    _fields_ = [("pcszFile", ctypes.c_wchar_p), ("pcszClass", ctypes.c_wchar_p),
                ("oaifInFlags", ctypes.c_uint32)]


def _load_shell32():
    if sys.platform != "win32":
        raise OSError("“以其他软件打开”需要 Windows 系统。")
    # Restrict DLL lookup to System32, independent of the opened file's folder.
    return ctypes.WinDLL("shell32.dll", use_last_error=True, winmode=0x00000800)


def open_with(path: Path, parent_handle: int = 0) -> bool:
    """Show the chooser. Cancellation is quiet; other native failures raise.

    A successful HRESULT means the chooser completed, not proof an external
    program launched. The UI deliberately does not show a success toast.
    """
    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError("图片文件暂时不可访问，请检查硬盘连接。")
    shell32 = _load_shell32()
    chooser = shell32.SHOpenWithDialog
    chooser.argtypes = [ctypes.c_void_p, ctypes.POINTER(OPENASINFO)]
    chooser.restype = ctypes.c_int32  # HRESULT is 32 bits on Windows x64.
    info = OPENASINFO(str(target), None, OAIF_EXEC | OAIF_HIDE_REGISTRATION)
    result = int(chooser(parent_handle or None, ctypes.byref(info))) & 0xFFFFFFFF
    if result in (HRESULT_CANCELLED, E_ABORT):
        return False
    if result & 0x80000000:
        raise OSError(f"无法打开 Windows 应用选择窗口（0x{result:08X}）。")
    return True
