"""Open Explorer and select one Unicode filename through the Windows Shell.

SHOpenFolderAndSelectItems accepts a fully qualified item PIDL with cidl=0,
opening its parent and selecting the item. No command string is reconstructed.
https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shopenfolderandselectitems
https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shparsedisplayname
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys

_HRESULT = ctypes.c_int32
_RPC_E_CHANGED_MODE = 0x80010106


def _check(result, message):
    value = int(result) & 0xffffffff
    if value & 0x80000000:
        raise OSError(f"{message}（0x{value:08X}）。")
    return value


def _load_native():
    if sys.platform != 'win32':
        raise OSError('定位并选中图片需要 Windows 系统。')
    # Search only trusted system DLLs, independently of the image directory.
    shell = ctypes.WinDLL('shell32.dll', use_last_error=True, winmode=0x800)
    ole = ctypes.WinDLL('ole32.dll', use_last_error=True, winmode=0x800)
    shell.SHParseDisplayName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
                                       ctypes.POINTER(ctypes.c_uint32)]
    shell.SHParseDisplayName.restype = _HRESULT
    shell.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_void_p, ctypes.c_uint32]
    shell.SHOpenFolderAndSelectItems.restype = _HRESULT
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = _HRESULT
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    return shell, ole


def reveal_in_folder(path: Path) -> bool:
    """Ask Explorer to select the file. Call from a worker, not the GUI thread.

    A successful HRESULT confirms Windows accepted the request; desktop state
    is independently checked by the Windows integration self-test.
    """
    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError('图片文件暂时不可访问，请检查硬盘连接。')
    shell, ole = _load_native()
    initialized = int(ole.CoInitializeEx(None, 2)) & 0xffffffff  # COINIT_APARTMENTTHREADED
    # An existing apartment still has COM initialized. Balance only successful
    # initialization calls, including S_FALSE; never uninitialize somebody else.
    if initialized != _RPC_E_CHANGED_MODE:
        _check(initialized, '无法初始化 Windows 文件定位')
    pointer = ctypes.c_void_p()
    try:
        _check(shell.SHParseDisplayName(str(target), None, ctypes.byref(pointer), 0, None),
               '无法读取所选图片的位置')
        if not pointer.value:
            raise OSError('Windows 未返回所选图片的位置。')
        _check(shell.SHOpenFolderAndSelectItems(pointer, 0, None, 0),
               '无法打开文件夹并选中图片')
        return True
    finally:
        if pointer.value:
            ole.CoTaskMemFree(pointer)
        if initialized in (0, 1):
            ole.CoUninitialize()
