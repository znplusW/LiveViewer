"""Native Windows Open With chooser for one file, without shell command text.

API and flags: https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shopenwithdialog
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path


OAIF_EXEC = 0x00000004
OAIF_HIDE_REGISTRATION = 0x00000020
HRESULT_CANCELLED = 0x800704C7
E_ABORT = 0x80004004


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
