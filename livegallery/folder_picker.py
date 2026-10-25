"""Choose existing photo folders without scanning or changing their contents.

Windows uses IFileOpenDialog's filesystem multi-selection. Other platforms, or
an unavailable native dialog, use a Qt directory dialog with the same contract.
ABI: microsoft/win32metadata, ShObjIdl_core.h (IFileOpenDialog/IShellItemArray).
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QAbstractItemView, QDialog, QFileDialog, QListView, QTreeView

from .system_open import (E_ABORT, GUID, HRESULT_CANCELLED, IID_ISHELLITEM,
                          _ComPtr, _HRESULT, _hresult)
from .ui_theme import bind_theme


CLSID_FILE_OPEN_DIALOG = GUID.parse('dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7')
IID_IFILE_OPEN_DIALOG = GUID.parse('d57c7288-d4ad-4768-be02-9d969532d960')
FOS_NOCHANGEDIR = 0x8
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
FOS_ALLOWMULTISELECT = 0x200
FOS_PATHMUSTEXIST = 0x800
FOLDER_OPTIONS = (FOS_NOCHANGEDIR | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
                  | FOS_ALLOWMULTISELECT | FOS_PATHMUSTEXIST)
SIGDN_FILESYSPATH = 0x80058000
TITLE = '选择照片文件夹（Ctrl／Shift 可多选）'


class _NativeDialogUnavailable(OSError):
    """Native setup failed before any chooser was shown; Qt may take over."""


def _initial_directory(initial):
    if initial is None:
        return None
    try:
        path = Path(initial).expanduser()
        return path.resolve() if path.is_dir() else None
    except OSError:
        return None


def _directories(paths):
    selected, seen = [], set()
    for value in paths:
        path = Path(value).resolve()
        if not path.is_dir():
            raise OSError('所选文件夹暂时不可访问，请检查路径或硬盘连接：' + str(path))
        identity = os.path.normcase(str(path))
        if identity not in seen:
            seen.add(identity)
            selected.append(path)
    return selected


def _native_libraries():
    ole = ctypes.WinDLL('ole32.dll', use_last_error=True, winmode=0x800)
    shell = ctypes.WinDLL('shell32.dll', use_last_error=True, winmode=0x800)
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = _HRESULT
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint32,
                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = _HRESULT
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    shell.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                 ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell.SHCreateItemFromParsingName.restype = _HRESULT
    return ole, shell


def _native_select_photo_folders(parent, initial):
    initialized, shown = False, False
    dialog = results = None
    try:
        ole, shell = _native_libraries()
        _hresult(ole.CoInitializeEx(None, 2), '无法准备文件夹选择窗口')  # STA
        initialized = True  # S_FALSE also requires a balancing CoUninitialize.
        pointer = ctypes.c_void_p()
        code = ole.CoCreateInstance(ctypes.byref(CLSID_FILE_OPEN_DIALOG), None, 1,
                                    ctypes.byref(IID_IFILE_OPEN_DIALOG), ctypes.byref(pointer))
        dialog = _ComPtr(pointer)
        _hresult(code, '无法创建文件夹选择窗口')
        if not dialog.address:
            raise OSError('Windows 未返回文件夹选择窗口。')
        options = ctypes.c_uint32()
        _hresult(dialog.call(10, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(options)),
                 '无法读取文件夹选择选项')
        _hresult(dialog.call(9, [ctypes.c_uint32], options.value | FOLDER_OPTIONS),
                 '无法启用文件夹多选')
        _hresult(dialog.call(17, [ctypes.c_wchar_p], TITLE), '无法设置文件夹窗口标题')
        _hresult(dialog.call(18, [ctypes.c_wchar_p], '选择文件夹'), '无法设置文件夹选择按钮')
        initial = _initial_directory(initial)
        if initial is not None:
            item_pointer = ctypes.c_void_p()
            code = shell.SHCreateItemFromParsingName(str(initial), None, ctypes.byref(IID_ISHELLITEM),
                                                     ctypes.byref(item_pointer))
            item = _ComPtr(item_pointer)
            try:
                if not int(code) & 0x80000000 and item.address:
                    _hresult(dialog.call(12, [ctypes.c_void_p], item.address),
                             '无法设置初始文件夹')
            finally:
                item.close()
        owner = int(parent.window().winId()) if parent is not None else 0
        shown = True
        code = int(dialog.call(3, [ctypes.c_void_p], owner or None)) & 0xffffffff
        if code in (HRESULT_CANCELLED, E_ABORT):
            return []
        _hresult(code, '文件夹选择未完成')
        output = ctypes.c_void_p()
        code = dialog.call(27, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(output))
        results = _ComPtr(output)
        _hresult(code, '无法读取所选文件夹')
        if not results.address:
            raise OSError('Windows 未返回所选文件夹。')
        count = ctypes.c_uint32()
        _hresult(results.call(7, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(count)),
                 '无法读取所选文件夹数量')
        selected = []
        for index in range(count.value):
            output = ctypes.c_void_p()
            code = results.call(8, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                                index, ctypes.byref(output))
            item = _ComPtr(output)
            name = ctypes.c_void_p()
            try:
                _hresult(code, '无法读取所选文件夹')
                if not item.address:
                    raise OSError('Windows 未返回所选文件夹对象。')
                _hresult(item.call(5, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                                   SIGDN_FILESYSPATH, ctypes.byref(name)), '无法读取文件夹路径')
                if not name.value:
                    raise OSError('所选文件夹没有有效的本地路径。')
                selected.append(ctypes.wstring_at(name.value))
            finally:
                if name.value:
                    ole.CoTaskMemFree(name)
                item.close()
        return _directories(selected)
    except (AttributeError, OSError) as error:
        if not shown:
            raise _NativeDialogUnavailable(str(error)) from error
        raise
    finally:
        if results is not None:
            results.close()
        if dialog is not None:
            dialog.close()
        if initialized:
            ole.CoUninitialize()


class _MultiFolderDialog(QFileDialog):
    def __init__(self, parent=None, initial=None):
        directory = _initial_directory(initial) or Path.home()
        super().__init__(parent, TITLE, str(directory))
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setOption(QFileDialog.Option.ShowDirsOnly, True)
        self.setOption(QFileDialog.Option.ReadOnly, True)
        self.setFileMode(QFileDialog.FileMode.Directory)
        self.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        self.setViewMode(QFileDialog.ViewMode.Detail)
        self.setLabelText(QFileDialog.DialogLabel.Accept, '选择文件夹')
        self.setLabelText(QFileDialog.DialogLabel.FileName, '文件夹（Ctrl／Shift 可多选）')
        for name, view_type in (('listView', QListView), ('treeView', QTreeView)):
            view = self.findChild(view_type, name)
            if view is not None:
                view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.selected_directories = []
        bind_theme(self)

    def accept(self):
        # QFileDialog's Directory acceptance may retain just its first result.
        # Parse all real selected paths and use QDialog's acceptance only after
        # validating every result, retaining ordinary navigation/double-click.
        try:
            selected = _directories(self.selectedFiles())
        except OSError:
            self.setLabelText(QFileDialog.DialogLabel.FileName, '请选择仍可访问的文件夹')
            return
        if selected:
            self.selected_directories = selected
            QDialog.accept(self)


def select_photo_folders(parent=None, initial: Path | None = None) -> list[Path]:
    """Return selected existing folders; cancellation returns an empty list."""
    if sys.platform == 'win32':
        try:
            return _native_select_photo_folders(parent, initial)
        except _NativeDialogUnavailable:
            pass
    dialog = _MultiFolderDialog(parent, initial)
    try:
        return list(dialog.selected_directories) if dialog.exec() == QDialog.DialogCode.Accepted else []
    finally:
        dialog.deleteLater()
