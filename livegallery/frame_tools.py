"""Locate the private frame tools without loading their DLLs into Qt."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys


@dataclass(frozen=True)
class FrameTools:
    ffmpeg: Path
    ffprobe: Path


def resolve_frame_tools(ffmpeg=None, ffprobe=None) -> FrameTools:
    if ffmpeg is not None or ffprobe is not None:
        if ffmpeg is None or ffprobe is None:
            raise ValueError("ffmpeg 和 ffprobe 必须同时指定。")
        result = FrameTools(Path(ffmpeg), Path(ffprobe))
    elif getattr(sys, 'frozen', False):
        # The isolated CLI folder carries its matching FFmpeg DLLs. Qt keeps
        # its own media DLLs in PySide6; never merge the two library trees.
        base = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent / '_internal')) / 'tools' / 'ffmpeg'
        result = FrameTools(base / 'ffmpeg.exe', base / 'ffprobe.exe')
    elif sys.platform == 'win32':
        base = Path(__file__).resolve().parents[1] / 'tools' / 'ffmpeg'
        result = FrameTools(base / 'ffmpeg.exe', base / 'ffprobe.exe')
    else:
        # Development only. The Windows portable application never depends on
        # the user's PATH, a Python installation, or a separately installed app.
        encoder = os.environ.get('LIVEVIEWER_FFMPEG') or shutil.which('ffmpeg')
        probe = os.environ.get('LIVEVIEWER_FFPROBE') or shutil.which('ffprobe')
        if not encoder or not probe:
            raise OSError("开发环境缺少 ffmpeg／ffprobe；Windows 绿色版自带读取组件。")
        result = FrameTools(Path(encoder), Path(probe))
    if not result.ffmpeg.is_file() or not result.ffprobe.is_file():
        raise OSError("视频逐帧读取组件缺失，请重新完整解压 LiveViewer。")
    return result
