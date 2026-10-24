"""Bounded local visual comparison with transparent confidence heuristics.

Scores are similarity measures, not calibrated probabilities. Conservative
thresholds deliberately surface ambiguous choices for manual correction.
"""
import math
import base64
import io
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .cache import CacheStore
from .imaging import load_image
from .library import preferred_image
from .models import MediaItem, PairDecision

ALGORITHM_VERSION = "visual-qt-v4"
_UUID = re.compile(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _array(image: Image.Image, edge: int = 96) -> np.ndarray:
    return np.asarray(image.resize((edge, edge), Image.Resampling.LANCZOS), dtype=np.float32) / 255.0


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _same_content(first: Image.Image, second: Image.Image) -> bool:
    if abs(math.log((first.width / first.height) / (second.width / second.height))) > 0.025:
        return False
    a, b = _array(first), _array(second)
    la, lb = _luma(a), _luma(b)
    # Permit small exposure differences, but retain local detail and colour.
    shift = float(np.clip((la - lb).mean(), -0.06, 0.06))
    luma_rmse = float(np.sqrt(np.mean((la - lb - shift) ** 2)))
    # JPEG 4:2:0 subsampling loses chroma detail even at high quality. Compare
    # colour at a lower resolution while preserving the stricter luma test.
    ac, bc = _array(first, 48), _array(second, 48)
    chroma_rmse = float(np.sqrt(np.mean(((ac - _luma(ac)[..., None]) - (bc - _luma(bc)[..., None])) ** 2)))
    edge_error = float(np.mean(np.abs(np.diff(la, axis=0) - np.diff(lb, axis=0))) +
                       np.mean(np.abs(np.diff(la, axis=1) - np.diff(lb, axis=1)))) / 2
    return luma_rmse < 0.024 and chroma_rmse < 0.032 and edge_error < 0.028


def _similarity(still: Image.Image, frame: Image.Image) -> float:
    # Compare both direct fitting and matching centre crops: phone video often
    # has a narrower view than the high-resolution still due to stabilisation.
    scores = []
    for crop in (False, True):
        if crop:
            target_ratio = frame.width / frame.height
            width, height = still.size
            if width / height > target_ratio:
                crop_width = height * target_ratio
                image = still.crop(((width - crop_width) / 2, 0, (width + crop_width) / 2, height))
            else:
                crop_height = width / target_ratio
                image = still.crop((0, (height - crop_height) / 2, width, (height + crop_height) / 2))
        else:
            image = still
        a, b = _array(image, 64), _array(frame, 64)
        la, lb = _luma(a), _luma(b)
        ca, cb = la - la.mean(), lb - lb.mean()
        denominator = float(np.sqrt(np.mean(ca ** 2) * np.mean(cb ** 2)))
        colour_error = float(np.mean(np.abs(a.mean(axis=(0, 1)) - b.mean(axis=(0, 1)))))
        if denominator < 1e-5:
            correlation = 1.0 if colour_error < 0.06 and float(np.std(la - lb)) < 0.02 else 0.0
        else:
            correlation = float(np.clip(np.mean(ca * cb) / denominator, 0, 1))
        residual = float(np.sqrt(np.mean((ca - cb) ** 2)))
        edge_error = float(np.mean(np.abs(np.diff(la, axis=0) - np.diff(lb, axis=0))) +
                           np.mean(np.abs(np.diff(la, axis=1) - np.diff(lb, axis=1)))) / 2
        scores.append(0.40 * correlation + 0.25 * math.exp(-8 * residual) +
                      0.20 * math.exp(-6 * colour_error) + 0.15 * math.exp(-10 * edge_error))
    return max(scores)


def _image_identifiers(path: Path) -> set[str]:
    # Apple records the shared identifier in its EXIF MakerNote. UUID lookup
    # also tolerates exporter-specific MakerNote layouts without parsing offsets.
    identifiers: set[str] = set()
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            values = [exif.get(0x927C, b""), exif.get_ifd(0x8769).get(0x927C, b"")]
            for key in ("xmp", "XML:com.adobe.xmp"):
                values.append(image.info.get(key, b""))
            for value in values:
                if isinstance(value, str):
                    value = value.encode()
                if isinstance(value, bytes):
                    identifiers.update(match.decode().casefold() for match in _UUID.findall(value))
    except (OSError, ValueError, TypeError, SyntaxError, struct.error, KeyError, IndexError):
        pass
    return identifiers


def _mov_identifiers(path: Path) -> set[str]:
    """Read bounded metadata boxes, seeking past media payloads on slow disks."""
    identifiers: set[str] = set()
    try:
        with path.open("rb") as source:
            length = path.stat().st_size
            position = 0
            while position + 8 <= length:
                source.seek(position)
                header = source.read(8)
                if len(header) < 8:
                    break
                size, kind = struct.unpack(">I4s", header)
                header_size = 8
                if size == 1:
                    extended = source.read(8)
                    if len(extended) < 8:
                        break
                    size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif size == 0:
                    size = length - position
                if size < header_size or position + size > length:
                    break
                if kind in {b"moov", b"udta", b"meta"}:
                    # A normal Live Photo metadata box is much smaller. The cap
                    # protects against malformed files and unusually long videos.
                    data = source.read(min(size - header_size, 8 * 1024 ** 2))
                    identifiers.update(match.decode().casefold() for match in _UUID.findall(data))
                position += size
    except OSError:
        pass
    return identifiers


class _ProbeClient:
    """One isolated Qt decoder shared by matching workers, with hard timeouts."""
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.messages = queue.Queue()
        self.sequence = 0

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        environment = dict(os.environ)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")
        command = ([sys.executable, "--probe-server"] if getattr(sys, "frozen", False) else
                   [sys.executable, str(Path(__file__).resolve().parents[1] / "launch.py"), "--probe-server"])
        self.messages = queue.Queue()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=environment,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process, messages = self.process, self.messages

        def reader():
            try:
                for line in process.stdout:
                    messages.put(line)
            except (OSError, ValueError):
                pass
            finally:
                messages.put(None)
        threading.Thread(target=reader, name="video-probe-output", daemon=True).start()

    def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def frames(self, path: Path) -> list[Image.Image]:
        with self.lock:
            try:
                self._start()
                self.sequence += 1
                request_id = self.sequence
                request = {"id": request_id, "path": str(path.absolute()),
                           "times": [0.03, 0.20, 0.35, 0.50, 0.65, 0.80, 0.95]}
                self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                deadline = time.monotonic() + 30
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Video analysis timed out")
                    line = self.messages.get(timeout=remaining)
                    if line is None:
                        raise OSError("Video probe exited")
                    try:
                        result = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(result, dict):
                        continue
                    if result.get("id") != request_id:
                        continue
                    frames = []
                    for encoded in result.get("frames", [])[:8]:
                        try:
                            with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                                frames.append(image.convert("RGB"))
                        except (ValueError, OSError, TypeError):
                            continue
                    return frames
            except (OSError, ValueError, TimeoutError, queue.Empty):
                self._stop()
                return []

    def close(self):
        # Wake a request currently blocked in Queue.get instead of making UI
        # shutdown wait for its full decoder timeout.
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        with self.lock:
            self._stop()


_probe_client = _ProbeClient()


def close_video_probe() -> None:
    _probe_client.close()


def _video_samples(path: Path) -> tuple[list[Image.Image], set[str]]:
    return _probe_client.frames(path), _mov_identifiers(path)


def _preferred(paths: list[Path], images: dict[Path, Image.Image]) -> Path:
    def area(path: Path) -> int:
        width, height = images[path].info.get("original_size", images[path].size)
        return width * height
    return min(paths, key=lambda path: (path.suffix.casefold() != ".heic",
                                      -area(path), str(path).casefold()))


def resolve_pair(item: MediaItem, cache: CacheStore) -> PairDecision:
    candidates = item.candidates or (item.image_path,)
    override = cache.get_override(item)
    cached = cache.get_decision(item, ALGORITHM_VERSION) if override is None else None
    if cached:
        return cached
    if len(candidates) == 1:
        decision = PairDecision(candidates[0], (), False, "按同目录同名文件配对")
        cache.put_decision(item, ALGORITHM_VERSION, decision)
        return decision
    images: dict[Path, Image.Image] = {}
    for path in candidates:
        try:
            images[path] = load_image(path, 384)
        except (OSError, ValueError, SyntaxError):
            continue
    if not images:
        return PairDecision(override or preferred_image(candidates), tuple(p for p in candidates if p != (override or preferred_image(candidates))),
                            True, "候选图片无法解码，请检查原文件")
    groups: list[list[Path]] = []
    for path, image in images.items():
        for group in groups:
            if _same_content(image, images[group[0]]):
                group.append(path)
                break
        else:
            groups.append([path])
    representatives = [_preferred(group, images) for group in groups]
    selected = override
    uncertain = False
    if override:
        reason = "使用手动选择的静态图片"
    elif len(groups) == 1:
        selected = representatives[0]
        reason = "候选图片视觉内容相同，优先 HEIC；其他格式选择分辨率较高者"
    else:
        try:
            frames, video_identifiers = _video_samples(item.video_path) if item.video_path else ([], set())
        except (OSError, ValueError):
            frames, video_identifiers = [], set()
        scores = {path: max((_similarity(images[path], frame) for frame in frames), default=0.0)
                  for path in representatives}
        metadata_matches = {path for path in representatives if video_identifiers & _image_identifiers(path)}
        ranked = sorted(representatives, key=lambda p: (-(scores[p] + (0.08 if p in metadata_matches else 0)),
                                                       p.suffix.casefold() != ".heic", str(p).casefold()))
        selected = ranked[0]
        first, second = scores[selected], scores[ranked[1]]
        metadata_advantage = selected in metadata_matches and len(metadata_matches) == 1
        uncertain = not frames or first < 0.73 or (first - second < 0.06 and not metadata_advantage)
        if not frames:
            reason = "无法读取视频画面，暂按 HEIC 优先选择，请手动确认"
        elif first < 0.55:
            reason = "候选图片与视频均缺少可靠视觉匹配，配对可能不正确"
        elif uncertain:
            reason = "自动择图存在歧义，请按需要手动改选"
        else:
            reason = "结合视频代表帧" + ("和原始关联标识" if metadata_advantage else "") + "选择更匹配的图片"
    chosen_group = next((group for group in groups if selected in group), [])
    extras = [_preferred(group, images) for group in groups if group is not chosen_group]
    # Undecodable files are retained rather than silently treated as duplicates.
    extras.extend(path for path in candidates if path not in images and path != selected)
    decision = PairDecision(selected, tuple(extras), uncertain or len(images) != len(candidates), reason,
                            len(groups) == 1 and len(images) == len(candidates))
    cache.put_decision(item, ALGORITHM_VERSION, decision)
    return decision
