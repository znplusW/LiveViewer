"""True HEVC HEIC export through an isolated, bundled encoder subprocess."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import threading
import time

from PIL import Image


def _executable() -> Path:
    name = 'LiveViewer.HeicEncoder' + ('.exe' if sys.platform == 'win32' else '')
    if getattr(sys, 'frozen', False):
        root = Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent / '_internal')) / 'tools/heic'
    else:
        root = Path(__file__).resolve().parents[1] / 'vendor/heic'
    path = root / name
    if not path.is_file():
        raise OSError('HEIC 保存组件缺失，请重新完整解压 LiveViewer。')
    return path


def encode_heic(image: Image.Image, destination: Path, *,
                cancel_event: threading.Event | None = None,
                exif: bytes | None = None) -> None:
    """Write genuine HEIC bytes; caller owns staging and final validation/commit."""
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('已取消 HEIC 保存。')
    if image.width < 1 or image.height < 1 or image.width * image.height > 100_000_000:
        raise ValueError('图片尺寸超出 HEIC 保存范围。')
    if exif is not None:
        if (not isinstance(exif, bytes) or not 14 <= len(exif) <= 60 * 1024
                or exif[:6] != b'Exif\x00\x00'
                or exif[6:10] not in (b'II*\x00', b'MM\x00*')):
            raise ValueError('HEIC EXIF 数据格式或长度无效。')
    rgb = image.convert('RGB')
    executable = _executable()
    pixels = rgb.tobytes()
    arguments = [str(executable), str(rgb.width), str(rgb.height), '95']
    if exif is not None:
        arguments.append(str(len(exif)))
        pixels += exif
    # stdout goes directly to the private stage, avoiding another encoded-file
    # copy in memory and preventing encoder diagnostics from becoming pixels.
    process = None
    created = False
    try:
        with Path(destination).open('xb') as target:
            created = True
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            process = subprocess.Popen(arguments,
                                       stdin=subprocess.PIPE, stdout=target, stderr=subprocess.PIPE,
                                       creationflags=flags)
            started = time.monotonic()
            first = True
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('已取消 HEIC 保存。')
                if time.monotonic() - started > 180:
                    raise TimeoutError('HEIC 编码超时，原图片未更改。')
                try:
                    _, errors = process.communicate(input=pixels if first else None, timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if process.returncode:
                message = errors.decode('utf-8', errors='replace')[-2000:]
                raise OSError('HEIC 编码失败，原图片未更改。' + message)
            target.flush()
            if target.tell() < 32:
                raise OSError('HEIC 编码结果为空，原图片未更改。')
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        if created:
            Path(destination).unlink(missing_ok=True)
        raise
