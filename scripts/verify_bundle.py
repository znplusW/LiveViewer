"""Verify the portable executable and the media DLLs actually shipped with it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]


def inspect(folder: Path) -> dict:
    exe = folder / "LiveViewer.exe"
    errors = []
    machine = None
    if not exe.is_file():
        errors.append("LiveViewer.exe is absent")
    else:
        with exe.open("rb") as stream:
            if stream.read(2) != b"MZ":
                errors.append("Executable has no DOS/PE header")
            else:
                stream.seek(0x3C)
                offset = struct.unpack("<I", stream.read(4))[0]
                stream.seek(offset)
                header = stream.read(6)
                if header[:4] != b"PE\x00\x00":
                    errors.append("Executable has no PE signature")
                else:
                    machine = struct.unpack("<H", header[4:6])[0]
                    if machine != 0x8664:
                        errors.append(f"Expected AMD64 PE machine 0x8664; received {machine:#x}")
    files = [path for path in folder.rglob("*") if path.is_file()]
    for path in files:
        lowered = str(path.relative_to(folder)).lower()
        if "av.libs" in lowered or path.name.lower().startswith(("libx264", "libx265")):
            errors.append(f"Development-only GPL encoder library leaked into bundle: {lowered}")
    patterns = {
        "qt_ffmpeg_backend": "ffmpegmediaplugin.dll",
        "ffmpeg_decoder": "avcodec-*.dll",
        "ffmpeg_demuxer": "avformat-*.dll",
        "ffmpeg_utility": "avutil-*.dll",
        "heic_decoder": "libheif-*.dll",
        "hevc_still_decoder": "libde265-*.dll",
        "windows_platform": "qwindows.dll",
        "third_party_manifest": "DEPENDENCIES.json",
    }
    found = {}
    for label, pattern in patterns.items():
        matches = [str(p.relative_to(folder)) for p in files if p.match(pattern)]
        found[label] = matches
        if not matches:
            errors.append(f"Missing {label}: {pattern}")
    # A Qt FFmpeg plugin must resolve to Qt's own matching, unrenamed libraries.
    qt_codec = [p for p in files if p.name.startswith("avcodec-") and p.parent.name == "PySide6"]
    if not qt_codec:
        errors.append("Qt's own FFmpeg DLLs were not retained under PySide6")
    licenses = [p for p in files if "licenses" in p.parts and p.suffix.lower() in {".txt", ".md"}]
    if len(licenses) < 5:
        errors.append("Third-party license collection is incomplete")
    return {
        "ok": not errors, "machine": hex(machine) if machine else None,
        "bundle": str(folder.resolve()), "files": len(files),
        "bytes": sum(p.stat().st_size for p in files), "required_components": found,
        "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest() if exe.is_file() else None,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/LiveViewer")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/bundle.json")
    args = parser.parse_args()
    report = inspect(args.bundle)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
