# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import importlib.util
import sysconfig
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH)
datas = [(str(root / 'licenses'), 'licenses'), (str(root / 'assets'), 'assets')]
# Keep private subprocess tools opaque to PyInstaller's dependency scanner.
# In particular, their FFmpeg DLLs must not be merged with Qt's matching DLLs.
for source, destination, executable in (
    (root / 'tools/ffmpeg', 'tools/ffmpeg', 'ffmpeg.exe'),
    (root / 'vendor/heic', 'tools/heic', 'LiveViewer.HeicEncoder.exe'),
):
    if not (source / executable).is_file():
        raise SystemExit(f'Missing private tool: {source / executable}')
    datas.append((str(source), destination))
for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
    if (root / name).is_file():
        datas.append((str(root / name), '.'))
binaries = []
hiddenimports = ['PIL.AvifImagePlugin', 'PIL.WebPImagePlugin', 'PIL.TiffImagePlugin']

# Qt's normal hooks select only modules actually imported by the application.
# Explicitly retain this Qt build's matching FFmpeg libraries as well.
pyside = Path(importlib.util.find_spec('PySide6').origin).parent
for pattern in ('avcodec-*.dll', 'avformat-*.dll', 'avutil-*.dll', 'swscale-*.dll', 'swresample-*.dll'):
    binaries.extend((str(path), 'PySide6') for path in pyside.glob(pattern))

# pi-heif's Windows wheel installs its renamed DLLs beside site-packages,
# rather than inside the Python package. Preserve the replaceable DLLs.
hiddenimports += collect_submodules('pi_heif')
for base in {Path(sysconfig.get_paths()['purelib']), Path(sysconfig.get_paths()['platlib'])}:
    for pattern in ('libheif-*.dll', 'libde265-*.dll', 'libgcc_s_seh-*.dll', 'libstdc++-*.dll', 'libwinpthread-*.dll'):
        binaries.extend((str(path), '.') for path in base.glob(pattern))

a = Analysis(
    [str(root / 'launch.py')], pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'av'], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='LiveViewer', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    disable_windowed_traceback=False, icon=str(root / 'assets/LiveViewer.ico'),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='LiveViewer')
