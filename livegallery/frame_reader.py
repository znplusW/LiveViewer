"""Cancellable exact decoded-frame access, including variable-rate videos.

ffprobe's decoded-frame list and FFmpeg's select(n) both use decoder output
order. Seeking by an estimated frame rate or by milliseconds is deliberately
avoided: neither can identify adjacent frames reliably in variable-rate video.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time

from PIL import Image

from .frame_tools import resolve_frame_tools


MAX_FRAMES = 20_000
MAX_DURATION_SECONDS = 600
MAX_PIXELS = 64_000_000
MAX_PROBE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_ERROR_BYTES = 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 45


class FrameCancelled(InterruptedError):
    """A cancelled request is recoverable; the next request can reuse a reader."""


@dataclass(frozen=True)
class FrameInfo:
    index: int
    pts_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class VideoMetadata:
    width: int
    height: int
    rotation: float
    frames: tuple[FrameInfo, ...]
    duration_seconds: float
    codec: str
    start_time_seconds: float
    timestamps_estimated: bool = False

    @property
    def frame_count(self):
        return len(self.frames)


def _number(value, fallback=None):
    try:
        number = float(value)
        return number if math.isfinite(number) else fallback
    except (TypeError, ValueError):
        return fallback


def _fraction(value, fallback=0.0):
    try:
        numerator, denominator = str(value).split('/', 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return fallback


class FrameReader:
    """One worker owns read operations; cancellation may come from the GUI.

    No media bytes are changed. Metadata is indexed once, previews are decoded
    on demand, and only one child process can run per reader. There is no frame
    image cache, keeping memory independent of the video's total frame count.
    """

    def __init__(self, path: Path, *, ffmpeg=None, ffprobe=None, cancel_event=None):
        self.path = Path(path).resolve()
        self._tool_arguments = (ffmpeg, ffprobe)
        self._tools = None
        self._cancel_event = cancel_event
        self._lock = threading.Lock()
        self._generation = 0
        self._process = None
        self._closed = False
        self._metadata = None
        self._signature = None

    @property
    def metadata(self) -> VideoMetadata:
        return self.open()

    @property
    def frame_count(self) -> int:
        return len(self.metadata.frames)

    def _check_cancelled(self, generation=None):
        with self._lock:
            cancelled = self._closed or (generation is not None and generation != self._generation)
        if cancelled or (self._cancel_event is not None and self._cancel_event.is_set()):
            raise FrameCancelled("已取消视频读取。")

    def _file_signature(self):
        status = self.path.stat()
        return status.st_size, status.st_mtime_ns

    def _verify_source(self):
        self._check_cancelled()
        if self._signature is not None and self._file_signature() != self._signature:
            raise OSError("视频文件已变化，请重新打开封面编辑器。")

    def _run(self, arguments, output_limit):
        self._check_cancelled()
        with self._lock:
            generation = self._generation
            if self._process is not None:
                raise RuntimeError("同一个视频读取器不能同时执行多个请求。")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        process = subprocess.Popen(
            [str(argument) for argument in arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
        )
        with self._lock:
            self._process = process
        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        try:
            while True:
                self._check_cancelled(generation)
                if time.monotonic() >= deadline:
                    raise TimeoutError("视频读取超时，请检查文件或移动硬盘连接。")
                try:
                    output, errors = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired as pending:
                    if (pending.output is not None and len(pending.output) > output_limit
                            or pending.stderr is not None and len(pending.stderr) > MAX_ERROR_BYTES):
                        raise ValueError("视频数据超过逐帧读取的资源上限。")
            self._check_cancelled(generation)
            if len(output) > output_limit or len(errors) > MAX_ERROR_BYTES:
                raise ValueError("视频数据超过逐帧读取的资源上限。")
            if process.returncode:
                detail = errors.decode('utf-8', 'replace').strip()[-1200:]
                raise OSError("无法读取视频帧。" + ("\n" + detail if detail else ""))
            return output
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            with self._lock:
                if self._process is process:
                    self._process = None

    def open(self) -> VideoMetadata:
        self._check_cancelled()
        if self._metadata is not None:
            self._verify_source()
            return self._metadata
        if not self.path.is_file():
            raise FileNotFoundError("视频文件暂时不可访问，请检查硬盘连接。")
        signature = self._file_signature()
        self._tools = resolve_frame_tools(*self._tool_arguments)
        command = [self._tools.ffprobe, '-v', 'error', '-threads', '1',
                   '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
                   '-show_frames', '-show_streams', '-show_format', '-show_entries',
                   'frame=best_effort_timestamp_time,pkt_duration_time,duration_time,width,height:'
                   'stream=width,height,codec_name,avg_frame_rate,duration:stream_tags=rotate:'
                   'stream_side_data=rotation:format=duration', '-of', 'json', str(self.path)]
        document = json.loads(self._run(command, MAX_PROBE_BYTES))
        streams, frames = document.get('streams', []), document.get('frames', [])
        if not streams or not frames:
            raise ValueError("视频中没有可读取的画面。")
        if len(frames) > MAX_FRAMES:
            raise ValueError(f"视频超过 {MAX_FRAMES:,} 帧，暂不支持在封面编辑器中读取。")
        stream = streams[0]
        width, height = int(stream.get('width', 0)), int(stream.get('height', 0))
        if width < 1 or height < 1 or width * height > MAX_PIXELS:
            raise ValueError("视频画面尺寸超过读取上限。")
        rotation = _number(stream.get('tags', {}).get('rotate'), 0.0)
        for data in stream.get('side_data_list', []):
            if 'rotation' in data:
                rotation = _number(data['rotation'], rotation)
        if round(rotation) % 180 == 90:
            width, height = height, width
        fps = _fraction(stream.get('avg_frame_rate'), 0.0)
        default_duration = 1.0 / fps if fps > 0 else 1.0 / 30
        timestamps, durations, estimated = [], [], False
        for frame in frames:
            timestamp = _number(frame.get('best_effort_timestamp_time'))
            duration = _number(frame.get('duration_time'), _number(frame.get('pkt_duration_time'), default_duration))
            duration = duration if duration > 0 else default_duration
            if timestamp is None:
                timestamp = timestamps[-1] + durations[-1] if timestamps else 0.0
                estimated = True
            if timestamps and timestamp < timestamps[-1] - .000001:
                raise ValueError("视频帧时间顺序异常，无法可靠逐帧选择。")
            timestamps.append(timestamp)
            durations.append(duration)
        start = timestamps[0]
        records = []
        for index, timestamp in enumerate(timestamps):
            interval = timestamps[index + 1] - timestamp if index + 1 < len(timestamps) else durations[index]
            records.append(FrameInfo(index, max(0.0, timestamp - start), max(0.0, interval)))
        duration = records[-1].pts_seconds + records[-1].duration_seconds
        if duration > MAX_DURATION_SECONDS:
            raise ValueError("视频超过 10 分钟，暂不支持在封面编辑器中读取。")
        if self._file_signature() != signature:
            raise OSError("视频文件已变化，请重新打开封面编辑器。")
        self._signature = signature
        self._metadata = VideoMetadata(width, height, rotation, tuple(records), duration,
                                       str(stream.get('codec_name', '')), start, estimated)
        return self._metadata

    def nearest_index(self, milliseconds: float) -> int:
        frames = self.metadata.frames
        seconds = max(0.0, float(milliseconds) / 1000)
        positions = [frame.pts_seconds for frame in frames]
        index = bisect_left(positions, seconds)
        if index == len(positions):
            return len(positions) - 1
        if index and seconds - positions[index - 1] <= positions[index] - seconds:
            return index - 1
        return index

    def _read(self, index: int, edge=None) -> Image.Image:
        metadata = self.open()
        if not isinstance(index, int) or not 0 <= index < metadata.frame_count:
            raise IndexError("视频帧编号超出范围。")
        self._verify_source()
        filters = [f'select=eq(n\\,{index})']
        if edge is not None:
            edge = max(64, min(4096, int(edge)))
            filters.append(f'scale=w=min({edge}\\,iw):h=min({edge}\\,ih):force_original_aspect_ratio=decrease')
        command = [self._tools.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                   '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file,pipe',
                   '-i', str(self.path), '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', ','.join(filters),
                   '-frames:v', '1', '-fps_mode', 'passthrough', '-c:v', 'png', '-f', 'image2pipe', 'pipe:1']
        content = self._run(command, MAX_IMAGE_BYTES)
        if not content:
            raise ValueError("未能读取所选视频帧。")
        with Image.open(io.BytesIO(content)) as decoded:
            if decoded.width * decoded.height > MAX_PIXELS:
                raise ValueError("视频画面尺寸超过读取上限。")
            image = decoded.convert('RGB')
            image.load()
        self._verify_source()
        return image

    def preview(self, index: int, edge=1200) -> Image.Image:
        return self._read(index, edge)

    def export(self, index: int) -> Image.Image:
        return self._read(index)

    def cancel_current(self):
        with self._lock:
            self._generation += 1
            process = self._process
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass

    def close(self):
        with self._lock:
            self._closed = True
        self.cancel_current()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_args):
        self.close()
