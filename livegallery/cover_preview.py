"""Bounded progressive cover previews and current-image frame matching.

Short Live Photos are decoded once into packed RGB previews. Navigation never
restarts that stream, and repeated requests cannot starve an in-flight preview.
Full-resolution saving remains separate in :mod:`frame_reader`.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image

from .frame_reader import FrameCancelled, FrameReader, PROCESS_TIMEOUT_SECONDS
from .frame_tools import resolve_frame_tools
from .imaging import load_image
from .matching import _array, _luma


MAX_CACHE_BYTES = 96 * 1024 * 1024
MAX_SEQUENTIAL_FRAMES = 360
MAX_LONG_SAMPLES = 120
MATCH_EDGE = 96
REFINE_DELAY_SECONDS = .14


def _cover_signatures(still, ratio):
    """Prepare FOV-tolerant still signatures once, off the preview thread.

    A Live Photo video commonly has a narrower stabilised field of view even
    when its aspect ratio equals the still. Aspect-ratio cropping alone cannot
    compensate for that; modest centre crops model that difference.
    """
    sources = [still]
    width, height = still.size
    if abs(width / height - ratio) > .001:
        if width / height > ratio:
            cropped_width = height * ratio
            sources.append(still.crop(((width - cropped_width) / 2, 0,
                                       (width + cropped_width) / 2, height)))
        else:
            cropped_height = width / ratio
            sources.append(still.crop((0, (height - cropped_height) / 2,
                                       width, (height + cropped_height) / 2)))
    signatures = []
    for source in sources:
        width, height = source.size
        for fraction in (1.0, .975, .95, .925, .90, .875, .85):
            dx, dy = width * (1 - fraction) / 2, height * (1 - fraction) / 2
            rgb = _array(source.crop((dx, dy, width - dx, height - dy)), 64)
            luma = _luma(rgb)
            centred = luma - luma.mean()
            signatures.append((centred, rgb.mean(axis=(0, 1)),
                               np.diff(luma, axis=0), np.diff(luma, axis=1),
                               float(np.mean(centred ** 2))))
    return signatures


def _cover_similarity(signatures, frame):
    # Same transparent score as matching._similarity, with prepared still
    # arrays so additional FOV candidates do not repeatedly resize a HEIC.
    rgb = _array(frame, 64)
    luma = _luma(rgb)
    centred = luma - luma.mean()
    mean = rgb.mean(axis=(0, 1))
    edge0, edge1 = np.diff(luma, axis=0), np.diff(luma, axis=1)
    variance = float(np.mean(centred ** 2))
    best = 0.0
    for source, colour, source_edge0, source_edge1, source_variance in signatures:
        denominator = math.sqrt(source_variance * variance)
        colour_error = float(np.mean(np.abs(colour - mean)))
        if denominator < 1e-5:
            correlation = 1.0 if colour_error < .06 and float(np.std(source - centred)) < .02 else 0.0
        else:
            correlation = float(np.clip(np.mean(source * centred) / denominator, 0, 1))
        residual = float(np.sqrt(np.mean((source - centred) ** 2)))
        edge_error = (float(np.mean(np.abs(source_edge0 - edge0))) +
                      float(np.mean(np.abs(source_edge1 - edge1)))) / 2
        score = (.40 * correlation + .25 * math.exp(-8 * residual) +
                 .20 * math.exp(-6 * colour_error) + .15 * math.exp(-10 * edge_error))
        best = max(best, score)
    return best


@dataclass(frozen=True)
class CoverPosition:
    index: int | None
    seconds: float | None
    approximate: bool = True
    confidence: float = 0.0
    ambiguous: bool = False
    reason: str = "根据当前封面图画面匹配"


class PreviewSession:
    """A progressive cache plus one latest-demand reader, owned by an editor.

    Callbacks run on workers, outside the cache lock. Only requested frames are
    published; sequential warming never floods Qt with unrequested images.
    ``get`` returns an exact cached frame, never a nearby frame represented as
    the selected one. Closing is nonblocking; ``wait`` is available to tests or
    worker-side cleanup.
    """

    supports_reader_fork = True

    def __init__(self, video_path, metadata, *, image_path=None, on_frame=None,
                 on_position=None, on_error=None, cancel_event=None,
                 max_bytes=MAX_CACHE_BYTES, edge=720, ffmpeg=None, ffprobe=None,
                 reader=None):
        self.path = Path(video_path).resolve()
        self.metadata = metadata
        self.image_path = Path(image_path) if image_path is not None else None
        self.on_frame = on_frame
        self.on_position = on_position
        self.on_error = on_error
        self._external_cancel = cancel_event
        self.max_bytes = max(4096, min(MAX_CACHE_BYTES, int(max_bytes)))
        self.edge = max(64, min(1200, int(edge)))
        self._tool_arguments = (ffmpeg, ffprobe)
        self._source_reader = reader
        if reader is not None and Path(reader.path).resolve() != self.path:
            raise ValueError("封面图预览读取器与视频文件不一致。")
        self._condition = threading.Condition(threading.RLock())
        self._closed = threading.Event()
        self._started = False
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._features = {}
        self._requested = None
        self._request_number = 0
        self._requested_at = 0.0
        self._process = None
        self._request_reader = None
        self._stream_done = False
        self._stream_error = None
        self._threads = []
        self._signature = self._file_signature(self.path)
        self._image_signature = (self._file_signature(self.image_path)
                                 if self.image_path is not None else None)
        count = len(metadata.frames)
        if not count or metadata.width < 1 or metadata.height < 1:
            raise ValueError("视频中没有可预览的封面图画面。")
        step = 1 if count <= MAX_SEQUENTIAL_FRAMES else math.ceil(count / MAX_LONG_SAMPLES)
        self._sample_indices = tuple(range(0, count, step))
        self._sample_set = frozenset(self._sample_indices)
        self._stream_size = self._preview_size(len(self._sample_indices))

    @staticmethod
    def _file_signature(path):
        status = Path(path).stat()
        return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns

    def _verify_source(self):
        self._check_cancelled()
        if self._file_signature(self.path) != self._signature:
            raise OSError("视频文件已变化，请重新打开封面图编辑器。")

    def _check_cancelled(self):
        if self._closed.is_set() or (self._external_cancel is not None and self._external_cancel.is_set()):
            raise FrameCancelled("已取消封面图预览。")

    def _preview_size(self, sample_count):
        # Store packed 3-byte RGB, not Pillow's generally 4-byte RGB ImageCore.
        # Leave headroom for one refined frame without evicting all neighbours.
        width, height = self.metadata.width, self.metadata.height
        available_pixels = self.max_bytes * .80 / max(1, sample_count) / 3
        scale = min(1.0, self.edge / max(width, height), math.sqrt(available_pixels / (width * height)))
        return max(1, int(width * scale)), max(1, int(height * scale))

    @property
    def cache_bytes(self):
        with self._condition:
            return self._cache_bytes

    @property
    def cached_indices(self):
        with self._condition:
            return tuple(self._cache)

    @property
    def preload_complete(self):
        with self._condition:
            return self._stream_done

    @property
    def preload_error(self):
        with self._condition:
            return self._stream_error

    def start(self):
        with self._condition:
            if self._started or self._closed.is_set():
                return
            self._started = True
            for name, target in (("cover-preview-stream", self._preload),
                                 ("cover-preview-demand", self._demand),
                                 ("cover-position", self._match_position)):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._threads.append(thread)
            threads = tuple(self._threads)
        for thread in threads:
            thread.start()

    def get(self, index):
        with self._condition:
            if self._closed.is_set():
                return None
            entry = self._cache.get(int(index))
            if entry is None:
                return None
            self._cache.move_to_end(int(index))
        size, pixels = entry
        return Image.frombytes('RGB', size, pixels)

    def request(self, index):
        index = int(index)
        if not 0 <= index < len(self.metadata.frames):
            raise IndexError("视频帧编号超出范围。")
        with self._condition:
            if self._closed.is_set():
                return
            self._requested = index
            self._request_number += 1
            self._requested_at = time.monotonic()
            self._condition.notify_all()

    def _store(self, index, image, *, matching=False):
        rgb = image.convert('RGB')
        entry = (rgb.size, rgb.tobytes())
        feature = None
        if matching:
            small = rgb.copy()
            small.thumbnail((MATCH_EDGE, MATCH_EDGE), Image.Resampling.BILINEAR)
            feature = (small.size, small.tobytes())
        with self._condition:
            if self._closed.is_set():
                return
            existing = self._cache.get(index)
            # A late small warming frame must not replace a refined preview.
            if existing is None or existing[0][0] * existing[0][1] <= rgb.width * rgb.height:
                old = self._cache.pop(index, None)
                if old is not None:
                    self._cache_bytes -= len(old[1])
                if len(entry[1]) <= self.max_bytes:
                    self._cache[index] = entry
                    self._cache_bytes += len(entry[1])
                while self._cache_bytes > self.max_bytes:
                    _, removed = self._cache.popitem(last=False)
                    self._cache_bytes -= len(removed[1])
            if feature is not None:
                self._features[index] = feature
            publish = matching and index == self._requested
            self._condition.notify_all()
        if publish:
            # A cold seek can still be indexing while this independent stream
            # reaches the requested frame. Publish that useful exact image now.
            self._callback(self.on_frame, index, rgb)

    def _callback(self, callback, *arguments):
        if callback is not None and not self._closed.is_set():
            callback(*arguments)

    def _demand(self):
        delivered = 0
        refined = 0
        last_image = None
        last_index = None
        try:
            while True:
                with self._condition:
                    while self._request_number == delivered and self._request_number == refined:
                        self._condition.wait(.1)
                        self._check_cancelled()
                    self._check_cancelled()
                    number, index = self._request_number, self._requested
                if index is None:
                    continue
                try:
                    self._verify_source()
                    image = self.get(index)
                    if image is None and delivered == number and last_index == index:
                        image = last_image
                    if number != delivered:
                        if image is None:
                            # Give the shared sequential decode a brief chance
                            # to produce an early frame before launching a seek.
                            with self._condition:
                                self._condition.wait(.035)
                                if number != self._request_number:
                                    # Choose the newest target now, then finish
                                    # it even if input keeps moving afterwards.
                                    number, index = self._request_number, self._requested
                            image = self.get(index)
                        if image is None:
                            image = self._reader().preview(index, edge=self.edge)
                            self._store(index, image)
                        # Completing one request provides visible progress while
                        # newer input continues. Never restart on every event.
                        self._callback(self.on_frame, index, image)
                        delivered = number
                        last_index, last_image = index, image
                    with self._condition:
                        if number != self._request_number:
                            continue
                        remaining = REFINE_DELAY_SECONDS - (time.monotonic() - self._requested_at)
                        if remaining > 0:
                            self._condition.wait(remaining)
                            continue
                    # Only a quiet selection gets a sharper preview. Continuous
                    # navigation uses cache lookups without process churn.
                    if refined != number:
                        if max(image.size) < min(1200, max(self.metadata.width, self.metadata.height)):
                            image = self._reader().preview(index, edge=1200)
                            self._store(index, image)
                            self._callback(self.on_frame, index, image)
                            last_index, last_image = index, image
                        refined = number
                except FrameCancelled:
                    raise
                except Exception as error:
                    # A failed target must not permanently kill this worker:
                    # subsequent selection can recover (e.g. a reconnect).
                    delivered = refined = number
                    if self._request_reader is not None:
                        self._request_reader.close()
                        self._request_reader = None
                    with self._condition:
                        current = number == self._request_number
                    if current:
                        self._callback(self.on_error, str(error))
        except FrameCancelled:
            pass
        finally:
            if self._request_reader is not None:
                self._request_reader.close()

    def _reader(self):
        if self._request_reader is None:
            if self._source_reader is not None:
                self._request_reader = self._source_reader.fork(cancel_event=self._closed)
            else:
                self._request_reader = FrameReader(self.path,
                                                   ffmpeg=self._tool_arguments[0],
                                                   ffprobe=self._tool_arguments[1],
                                                   cancel_event=self._closed)
        return self._request_reader

    def _preload(self):
        process = None
        timer = None
        error_thread = None
        errors = bytearray()
        timed_out = threading.Event()
        try:
            self._verify_source()
            tools = resolve_frame_tools(*self._tool_arguments)
            width, height = self._stream_size
            filters = []
            step = self._sample_indices[1] if len(self._sample_indices) > 1 else 1
            if step > 1:
                filters.append(f'select=not(mod(n\\,{step}))')
            filters.append(f'scale={width}:{height}')
            arguments = [tools.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                         '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file,pipe',
                         '-i', str(self.path), '-map', '0:v:0', '-an', '-sn', '-dn',
                         '-vf', ','.join(filters), '-frames:v', str(len(self._sample_indices)),
                         '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            process = subprocess.Popen([str(value) for value in arguments], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
            with self._condition:
                self._process = process
                self._check_cancelled()

            def drain_errors():
                while chunk := process.stderr.read(4096):
                    if len(errors) < 8192:
                        errors.extend(chunk[:8192 - len(errors)])

            error_thread = threading.Thread(target=drain_errors, name='cover-preview-errors', daemon=True)
            error_thread.start()

            def stop_timeout():
                timed_out.set()
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass

            timer = threading.Timer(PROCESS_TIMEOUT_SECONDS, stop_timeout)
            timer.daemon = True
            timer.start()
            frame_bytes = width * height * 3
            for index in self._sample_indices:
                self._check_cancelled()
                pixels = process.stdout.read(frame_bytes)
                if len(pixels) != frame_bytes:
                    self._check_cancelled()
                    raise OSError('封面图预览缓存未能读取完整画面。')
                self._store(index, Image.frombytes('RGB', (width, height), pixels), matching=True)
            process.wait(timeout=5)
            self._check_cancelled()
            if timed_out.is_set():
                raise TimeoutError('封面图预览缓存读取超时。')
            if process.returncode:
                raise OSError('封面图预览缓存读取失败。' + errors.decode('utf-8', 'replace')[-1000:])
            self._verify_source()
        except FrameCancelled:
            pass
        except Exception as error:
            with self._condition:
                self._stream_error = str(error)
        finally:
            if timer is not None:
                timer.cancel()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                if error_thread is not None:
                    error_thread.join(timeout=1)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            with self._condition:
                self._process = None
                self._stream_done = True
                self._condition.notify_all()

    def _match_position(self):
        if self.on_position is None:
            return
        marker = getattr(self.metadata, 'still_image_time', None)
        identifier = getattr(self.metadata, 'asset_identifier', None)
        exact = [(frame.index, getattr(frame, 'presentation_time', None)) for frame in self.metadata.frames]
        if (marker is not None and identifier and self.image_path is not None
                and exact and all(value is not None for _, value in exact)):
            try:
                from .live_photo_image import read_content_identifier
                self._verify_source()
                image_identifier = read_content_identifier(self.image_path)
                if self._file_signature(self.image_path) != self._image_signature:
                    raise OSError('封面图已发生变化。')
                last = self.metadata.frames[-1]
                if image_identifier == identifier and exact[0][1] <= marker <= exact[-1][1] + max(0, getattr(last, 'duration_seconds', 0)):
                    index = min(exact, key=lambda row: (abs(row[1] - marker), row[0]))[0]
                    self._callback(self.on_position, CoverPosition(index,
                        self.metadata.frames[index].pts_seconds, approximate=False, confidence=1.0,
                        reason='读取视频中的实际封面时间标记。'))
                    return
            except FrameCancelled:
                return
            except OSError:
                return
            except ValueError:
                # An unreadable or missing image identity cannot authenticate
                # the movie's marker as the current still image's position.
                pass
        if self.image_path is None:
            return
        scores = {}
        try:
            # This is independent from initial preview decoding and the demand
            # worker. Loading a large HEIC cannot block the first visible frame.
            still = load_image(self.image_path, 512)
            signatures = _cover_signatures(still, self.metadata.width / self.metadata.height)
            if self._file_signature(self.image_path) != self._image_signature:
                raise OSError('封面图已发生变化。')
            while True:
                self._check_cancelled()
                with self._condition:
                    pending = [(index, entry) for index, entry in self._features.items() if index not in scores]
                    done = self._stream_done
                    if not pending and not done:
                        self._condition.wait(.1)
                        continue
                for index, (size, pixels) in pending:
                    self._check_cancelled()
                    scores[index] = _cover_similarity(signatures, Image.frombytes('RGB', size, pixels))
                if done:
                    break
            self._verify_source()
            if self._file_signature(self.image_path) != self._image_signature:
                raise OSError('封面图已发生变化。')
            if not scores:
                raise ValueError('未能读取用于匹配的画面。')
            ranked = sorted(scores, key=lambda index: (-scores[index], index))
            best = ranked[0]
            score = scores[best]
            if score < .72:
                result = CoverPosition(None, None, confidence=score, ambiguous=True,
                                       reason='当前封面图与视频画面差异较大，未标记对应位置。')
            else:
                gap = score - scores[ranked[1]] if len(ranked) > 1 else 1.0
                result = CoverPosition(best, self.metadata.frames[best].pts_seconds,
                                       confidence=score, ambiguous=gap < .015,
                                       reason='按当前封面图画面匹配，位置为近似值。')
            self._callback(self.on_position, result)
        except FrameCancelled:
            pass
        except Exception:
            self._callback(self.on_position, CoverPosition(None, None, ambiguous=True,
                                                          reason='未能匹配原封面图位置。'))

    def close(self):
        self._closed.set()
        with self._condition:
            process = self._process
            reader = self._request_reader
            self._cache.clear()
            self._cache_bytes = 0
            self._features.clear()
            self._condition.notify_all()
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        if reader is not None:
            reader.cancel_current()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        for thread in self._threads:
            if thread is threading.current_thread():
                return False
            thread.join(None if deadline is None else max(0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._threads)
