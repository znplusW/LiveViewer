"""Create small synthetic media; no personal photographs are used."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw
import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SIZE = (640, 420)


def picture(index: int = 9) -> Image.Image:
    image = Image.new("RGB", SIZE, "#183442")
    draw = ImageDraw.Draw(image)
    for y in range(SIZE[1]):
        draw.line((0, y, SIZE[0], y), fill=(24 + y // 8, 52 + y // 6, 66 + y // 5))
    draw.rounded_rectangle((60 + index * 10, 110, 220 + index * 10, 270), radius=30, fill="#f1b86b")
    draw.ellipse((420, 55, 525, 160), fill="#bce8de")
    draw.text((28, 365), "LIVE GALLERY / SYNTHETIC MEDIA", fill="white")
    return image


def codec_name(path: Path) -> str:
    with av.open(str(path)) as container:
        video = container.streams.video[0]
        name = video.codec_context.name
        frame = next(container.decode(video))
        if frame.width < 1 or frame.height < 1:
            raise ValueError(f"No decodable frame in {path}")
        return name


def write_video(path: Path, requested: str) -> str:
    if path.is_file():
        actual = codec_name(path)
        if actual == requested:
            return actual
        raise ValueError(f"Existing fixture {path} is {actual}, expected {requested}")
    encoder = {"h264": "libx264", "hevc": "libx265"}[requested]
    try:
        av.Codec(encoder, "w")
        with av.open(str(path), "w", format="mov") as container:
            stream = container.add_stream(encoder, rate=15)
            stream.width, stream.height = SIZE
            stream.pix_fmt = "yuv420p"
            if requested == "hevc":
                stream.codec_context.options = {"preset": "ultrafast", "crf": "28", "x265-params": "log-level=error:pools=1:frame-threads=1"}
                stream.codec_context.codec_tag = "hvc1"
            else:
                stream.codec_context.options = {"preset": "ultrafast", "crf": "24"}
            for index in range(21):
                frame = av.VideoFrame.from_ndarray(np.asarray(picture(index)), format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    except Exception:
        path.unlink(missing_ok=True)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(f"Neither PyAV's {encoder} nor an ffmpeg executable can generate {requested}")
        with tempfile.TemporaryDirectory(prefix="livegallery-fixture-") as temporary:
            frame_dir = Path(temporary)
            for index in range(21):
                picture(index).save(frame_dir / f"frame-{index:03}.png")
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "15", "-i", str(frame_dir / "frame-%03d.png"), "-c:v", encoder, "-pix_fmt", "yuv420p"]
            if requested == "hevc":
                command += ["-tag:v", "hvc1", "-x265-params", "log-level=error:pools=1"]
            command.append(str(path))
            subprocess.run(command, check=True)
    actual = codec_name(path)
    if actual != requested:
        raise ValueError(f"Generated {actual}, expected {requested}")
    return actual


def generate(folder: Path, require_codecs: bool) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    still = picture()
    images = []
    videos = []
    for filename in ("Live_H264.jpg", "Live_HEVC.jpg", "JPEG_PAIR.jpeg"):
        path = folder / filename
        if not path.is_file():
            still.save(path, quality=95)
        images.append(filename)
    for ext in ("png", "webp", "bmp", "gif", "tiff", "avif"):
        path = folder / f"ordinary.{ext}"
        if not path.is_file():
            still.save(path)
        # Validate decoding rather than assuming extension implies support.
        with Image.open(path) as check:
            check.load()
        images.append(path.name)
    failures = []
    for filename, codec in (("Live_H264.MOV", "h264"), ("Live_HEVC.MOV", "hevc"), ("JPEG_PAIR.mov", "h264")):
        try:
            actual = write_video(folder / filename, codec)
            videos.append({"path": filename, "codec": actual})
        except Exception as error:
            failures.append({"path": filename, "requested_codec": codec, "error": str(error)})
    try:
        from pi_heif import register_heif_opener
        register_heif_opener()
        heics = []
        for path in sorted(folder.rglob("*")):
            if path.suffix.lower() == ".heic":
                with Image.open(path) as image:
                    image.load()
                heics.append(str(path.relative_to(folder)))
    except ImportError:
        heics = []
    images = sorted(set(images) | {
        str(path.relative_to(folder)) for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"}
    })
    manifest = {"synthetic": True, "size": list(SIZE), "images": images, "videos": videos, "heic_images": heics, "generation_failures": failures}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if require_codecs and failures:
        raise RuntimeError(json.dumps(failures, indent=2))
    if require_codecs and not heics:
        raise RuntimeError("No committed synthetic .heic fixture: create one on Mac with sips before building.")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "tests/fixtures")
    parser.add_argument("--require-codecs", action="store_true", help="Fail unless H.264, HEVC and a committed HEIC image all decode")
    args = parser.parse_args()
    print(json.dumps(generate(args.output, args.require_codecs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
