"""Click the real Windows native Save button, then inspect the exported pixels."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import traceback
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from livegallery.cover_editor import EditorDialog
from livegallery.imaging import load_image
from livegallery.models import MediaItem


def click_native_save(stop, trace):
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetDlgItem.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindowEnabled.argtypes = [wintypes.HWND]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                                         ctypes.POINTER(ctypes.c_size_t)]
    user.SendMessageTimeoutW.restype = wintypes.LPARAM
    def text(hwnd, class_name=False):
        value = ctypes.create_unicode_buffer(512)
        (user.GetClassNameW if class_name else user.GetWindowTextW)(hwnd, value, len(value))
        return value.value
    deadline = time.monotonic() + 20
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            found = []
            @callback_type
            def collect(hwnd, _):
                process = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
                if (process.value == os.getpid() and user.IsWindowVisible(hwnd)
                        and text(hwnd, True) == '#32770' and text(hwnd) == '导出静帧画面'):
                    found.append(hwnd)
                return True
            user.EnumWindows(collect, 0)
            if not found:
                stop.wait(.05)
                continue
            hwnd = found[0]
            button = user.GetDlgItem(hwnd, 1)
            if not button or not user.IsWindowEnabled(button):
                stop.wait(.05)
                continue
            trace.update(native_dialog_class=text(hwnd, True), native_dialog_title=text(hwnd),
                         save_button_class=text(button, True), save_button_text=text(button),
                         real_native_save_clicked=True)
            result = ctypes.c_size_t()
            trace['click_dispatch_result'] = int(user.SendMessageTimeoutW(
                button, 0x00F5, 0, 0, 0x0002, 5000, ctypes.byref(result)))
            return
        if not stop.is_set():
            trace['error'] = 'A real native SaveAs dialog with an enabled Save button was not found'
    except Exception:
        trace['error'] = traceback.format_exc()
    finally:
        if trace.get('error'):
            owned = []
            @callback_type
            def close_owned(hwnd, _):
                process = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
                if process.value == os.getpid() and text(hwnd, True) == '#32770':
                    owned.append({'title':text(hwnd),'class':text(hwnd,True)})
                    user.PostMessageW(hwnd, 0x0010, 0, 0)
                return True
            user.EnumWindows(close_owned, 0)
            trace['timed_out_native_windows'] = owned


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, default=Path('reports/native-export.json'))
    parser.add_argument('--readonly-directory', action='store_true')
    args = parser.parse_args()
    report = {'status': 'failed', 'real_native_save_clicked': False}
    if sys.platform != 'win32':
        raise RuntimeError('This probe requires Windows')
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, False)
    report.update(qt_platform=app.platformName(), style=app.style().objectName())
    assert app.platformName() == 'windows'
    dialog = worker = None
    stop = threading.Event()
    original_data = os.environ.get('LIVEGALLERY_DATA_DIR')
    with tempfile.TemporaryDirectory(prefix='LiveViewer-导出-') as directory:
        folder = Path(directory) / '中文路径 with spaces'
        folder.mkdir()
        os.environ['LIVEGALLERY_DATA_DIR'] = str(Path(directory) / 'data')
        still, video = folder / '测试静帧.jpg', folder / '测试静帧.MOV'
        fixtures = Path(__file__).parent / 'fixtures'
        shutil.copy2(fixtures / 'VFR_PAIR.jpg', still)
        shutil.copy2(fixtures / 'VFR_PAIR.MOV', video)
        export_folder = Path(directory) / '另一个导出目录 with spaces'
        export_folder.mkdir()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        kernel.GetFileAttributesW.restype = wintypes.DWORD
        kernel.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        kernel.SetFileAttributesW.restype = wintypes.BOOL
        original_attributes = int(kernel.GetFileAttributesW(str(export_folder)))
        try:
            assert original_attributes != 0xffffffff
            if args.readonly_directory:
                assert kernel.SetFileAttributesW(str(export_folder), original_attributes | 0x1)
                report['destination_readonly_attribute'] = True
                proof = export_folder / 'write-permission-proof.tmp'
                proof.write_bytes(b'Windows directory readonly is not an ACL denial')
                proof.unlink()
            dialog = EditorDialog(MediaItem('native-export', still, video), initial_export_directory=export_folder)
            dialog.show()
            def wait_for(condition, seconds=25):
                deadline = time.monotonic() + seconds
                while not condition() and time.monotonic() < deadline:
                    app.processEvents()
                    QTest.qWait(10)
                assert condition(), 'Timed out: ' + dialog.status_label.text()
            wait_for(lambda: dialog.metadata is not None and dialog.save_button.isEnabled())
            dialog.timeline.setValue(4)
            wait_for(lambda: dialog.selected_frame_index == dialog.displayed_frame_index == 4 and dialog._can_export())
            worker = threading.Thread(target=click_native_save, args=(stop, report), daemon=True)
            worker.start()
            def activate_export():
                menu = QApplication.activePopupWidget()
                if not isinstance(menu, QMenu):
                    report['error'] = 'Real export context menu did not open'
                    return
                action = next(a for a in menu.actions() if a.text().startswith('导出静帧画面'))
                QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
            QTimer.singleShot(0, activate_export)
            dialog._preview_menu(dialog.preview.rect().center())
            wait_for(lambda: bool(dialog.last_export_path or report.get('error')
                                  or dialog.last_export_error), 30)
            report.update(editor_status=dialog.status_label.text(), export_feedback=dialog.export_status_label.text(),
                          last_export_path=str(dialog.last_export_path) if dialog.last_export_path else None,
                          files=[p.name for p in folder.iterdir() if p.is_file()],
                          export_files=[p.name for p in export_folder.iterdir() if p.is_file()])
            assert not report.get('error'), report.get('error')
            assert report['real_native_save_clicked'], 'Native Save button was not clicked'
            assert dialog.last_export_path is not None, dialog.status_label.text()
            target = Path(dialog.last_export_path)
            assert target.is_file(), 'Exported output file is absent'
            assert target.parent.resolve() == export_folder.resolve(), 'Export ignored the chosen other directory'
            if args.readonly_directory:
                assert int(kernel.GetFileAttributesW(str(export_folder))) & 0x1, 'Exporter modified the directory attribute'
            report['other_directory_export_verified'] = True
            image = load_image(target, None)
            assert image.size == (320, 240)
            expected, actual = (207, 66, 194), image.getpixel((160, 120))
            assert max(abs(a-b) for a,b in zip(actual,expected)) < 10
            if sys.platform == 'win32':
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
                kernel.GetFileAttributesW.restype = wintypes.DWORD
                attributes = int(kernel.GetFileAttributesW(str(target)))
                report['output_attributes'] = attributes
                assert attributes != 0xffffffff and not attributes & (0x2 | 0x4), 'Export is hidden/system'
            report.update(status='passed', frame_index=4, full_size=list(image.size),
                          output_bytes=target.stat().st_size)
        except Exception as error:
            report.update(error=str(error), traceback=traceback.format_exc())
        finally:
            kernel.SetFileAttributesW(str(export_folder), original_attributes)
            stop.set()
            if worker is not None:
                worker.join(6)
            if dialog is not None:
                dialog.reject()
                deadline = time.monotonic() + 25
                while (dialog._worker.thread.is_alive() or
                       (dialog._export_thread and dialog._export_thread.is_alive())) and time.monotonic() < deadline:
                    app.processEvents()
                    QTest.qWait(10)
                report['workers_stopped'] = not dialog._worker.thread.is_alive() and not (
                    dialog._export_thread and dialog._export_thread.is_alive())
                if not report['workers_stopped']:
                    report.update(status='failed', cleanup_error='Editor workers did not stop before cleanup')
            if original_data is None:
                os.environ.pop('LIVEGALLERY_DATA_DIR', None)
            else:
                os.environ['LIVEGALLERY_DATA_DIR'] = original_data
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status']=='passed' else 1

if __name__ == '__main__':
    raise SystemExit(main())
