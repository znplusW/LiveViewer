"""Verify the portable executable and the media DLLs actually shipped with it."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct

if __package__:
    from . import build_windows, fetch_exiftool
else:
    import build_windows
    import fetch_exiftool

ROOT = Path(__file__).resolve().parents[1]


def inspect_exiftool(folder: Path, pin: dict | None = None) -> list[str]:
    errors = []
    pin = pin if pin is not None else fetch_exiftool.load_pin()
    internal = folder / '_internal'
    isolated = internal / 'tools/exiftool'
    try:
        fetch_exiftool.validate(isolated, pin)
    except (OSError, ValueError) as error:
        errors.append(str(error))
    private_names = {Path(name).name.casefold() for name in pin['runtime_files']
                     if Path(name).suffix.casefold() in ('.dll', '.exe')
                     and len(Path(name).parts) <= 2}
    for path in internal.rglob('*'):
        if path.is_file() and path.name.casefold() in private_names and not path.is_relative_to(isolated):
            errors.append(f'ExifTool/Perl binary escaped its isolated directory: {path.relative_to(internal).as_posix()}')
    notices = internal / 'licenses/ExifTool'
    expected = fetch_exiftool.provenance(pin)
    provenance = notices / 'PROVENANCE.json'
    try:
        if json.loads(provenance.read_text(encoding='utf8')) != expected:
            errors.append('ExifTool provenance differs from the accepted dependency pin')
    except (OSError, ValueError):
        errors.append('ExifTool provenance is absent or invalid')
    for name, expected_hash in expected['license_files'].items():
        path = notices / name
        if not path.is_file() or fetch_exiftool.sha256(path) != expected_hash:
            errors.append(f'ExifTool original notice is missing or changed: {name}')
    return errors


def inspect_media_isolation(folder: Path, qt_source: Path) -> list[str]:
    """Verify both DLL namespaces and Qt's original bytes, not just names."""
    errors = []
    internal = folder / '_internal'
    qt_folder = internal / 'PySide6'
    media_prefixes = ('avcodec-', 'avformat-', 'avutil-', 'avdevice-', 'avfilter-',
                      'swresample-', 'swscale-')
    for path in internal.rglob('*.dll'):
        name = path.name.casefold()
        relative = path.relative_to(internal).as_posix()
        if name.startswith(media_prefixes) and path.parent not in (qt_folder, internal / 'tools/ffmpeg'):
            errors.append(f'Media DLL escaped its isolated directory: {relative}')
        if name in {'heif.dll', 'kvazaar.dll'} and path.parent != internal / 'tools/heic':
            errors.append(f'HEIC encoder DLL escaped its isolated directory: {relative}')
    for pattern in ('avcodec-*.dll', 'avformat-*.dll', 'avutil-*.dll', 'swresample-*.dll', 'swscale-*.dll'):
        expected = list(qt_source.glob(pattern))
        actual = list(qt_folder.glob(pattern))
        if len(expected) != 1 or {p.name for p in actual} != {p.name for p in expected}:
            errors.append(f'Qt media DLL set differs from the installed Qt distribution: {pattern}')
    for path in qt_folder.rglob('*.dll'):
        relative = path.relative_to(qt_folder)
        original = qt_source / relative
        if not original.is_file() or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(original.read_bytes()).digest():
            errors.append(f'Qt DLL differs from the installed Qt distribution: {relative.as_posix()}')
    return errors


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
        relative = path.relative_to(folder)
        lowered = str(relative).lower()
        if (build_windows.is_runtime_path(relative)
                and not build_windows.is_pinned_perl_data_file(path, relative)):
            errors.append(f"Runtime user data must not be distributed: {relative.as_posix()}")
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
        "exact_frame_ffmpeg": "ffmpeg.exe",
        "exact_frame_ffprobe": "ffprobe.exe",
        "heic_encoder": "LiveViewer.HeicEncoder.exe",
        "live_photo_metadata_tool": "exiftool.exe",
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
    qt_spec = importlib.util.find_spec('PySide6')
    if qt_spec is None or qt_spec.origin is None:
        errors.append('Installed PySide6 distribution is required to verify Qt DLL sources')
    else:
        errors.extend(inspect_media_isolation(folder, Path(qt_spec.origin).parent))
    frame_folder = folder / '_internal/tools/ffmpeg'
    provenance_path = frame_folder / 'PROVENANCE.json'
    frame_tools = None
    if not provenance_path.is_file():
        errors.append('Pinned frame-tool provenance is absent from its isolated folder')
    else:
        frame_tools = json.loads(provenance_path.read_text(encoding='utf-8'))
        if frame_tools.get('runtime_validation', {}).get('status') != 'passed':
            errors.append('Pinned frame tools did not pass native LGPL/configuration validation')
        for binary in frame_tools.get('binaries', []):
            path = frame_folder / binary['file']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != binary['sha256']:
                errors.append(f'Private frame-tool binary differs from the pinned distribution: {binary["file"]}')
    heic_folder = folder / '_internal/tools/heic'
    for pattern in ('*heif*.dll', '*kvazaar*.dll', 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        if not list(heic_folder.glob(pattern)):
            errors.append(f'Missing isolated HEIC shared library/runtime: {pattern}')
    heic_provenance = folder / '_internal/licenses/HEIC/provenance.json'
    if not heic_provenance.is_file():
        errors.append('HEIC encoder provenance is absent')
    else:
        heic_record = json.loads(heic_provenance.read_text(encoding='utf-8'))
        if not heic_record.get('shared_libraries') or heic_record.get('x265') or heic_record.get('gpl_encoder'):
            errors.append('HEIC encoder must use replaceable shared libraries without a GPL encoder')
        for name, digest in heic_record.get('binaries', {}).items():
            path = heic_folder / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                errors.append(f'HEIC encoder binary differs from its source-build provenance: {name}')
    licenses = [p for p in files if "licenses" in p.parts and p.suffix.lower() in {".txt", ".md"}]
    errors.extend(inspect_exiftool(folder))
    if len(licenses) < 5:
        errors.append("Third-party license collection is incomplete")
    return {
        "ok": not errors, "machine": hex(machine) if machine else None,
        "bundle": str(folder.resolve()), "files": len(files),
        "bytes": sum(p.stat().st_size for p in files), "required_components": found,
        "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest() if exe.is_file() else None,
        "frame_tool_archive_sha256": frame_tools.get('sha256') if frame_tools else None,
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
