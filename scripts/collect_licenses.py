"""Retain distribution licenses, native-library notices and source references.

This deliberately inventories native DLLs as well as Python distribution metadata.
The development-only PyAV encoder stack is excluded from the application.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys
import sysconfig
import urllib.request

if __package__:
    from . import fetch_exiftool
else:
    import fetch_exiftool

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6", "Pillow", "pi-heif", "numpy")
UPSTREAM = {
    "Python": ("https://github.com/python/cpython", "PSF-2.0 and included notices"),
    "Qt-PySide6": ("https://download.qt.io/archive/qt/6.12/6.12.0/single/", "LGPL-3.0 and third-party notices"),
    "FFmpeg": ("https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz", "Qt build LGPL-2.1-or-later and included permissive third-party notices"),
    "FFmpeg-frame-tools": ("https://github.com/FFmpeg/FFmpeg/tree/2a571b606854520cf89804d8030c8b328e621689", "BtbN pinned LGPL shared variant; source/build references and original license in FFmpeg-CLI/"),
    "HEIC-encoder": ("dependencies/heic-encoder.json and native/heic_encoder/ in the corresponding source ZIP", "MIT helper, separate LGPL libheif and BSD Kvazaar shared libraries; exact pins/notices in HEIC/"),
    "ExifTool": ("https://exiftool.org/", "Separate ExifTool/Perl subprocess runtime; complete original licenses, source project links and accepted file hashes in ExifTool/"),
    "MinGW-runtime": ("https://www.mingw-w64.org/", "GCC runtime exception, MIT and BSD; see upstream notices"),
}
TEXTS = {
    "Python-LICENSE.txt": f"https://raw.githubusercontent.com/python/cpython/v{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}/LICENSE",
    "LGPL-3.0.txt": "https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/LGPL-3.0-only.txt",
    "GPL-3.0.txt": "https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/GPL-3.0-only.txt",
    "FFmpeg-LGPL-2.1.txt": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/COPYING.LGPLv2.1",
    "FFmpeg-GPL-2.0.txt": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/COPYING.GPLv2",
    "FFmpeg-LICENSE.md": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/LICENSE.md",
    "GCC-runtime-exception.txt": "https://raw.githubusercontent.com/gcc-mirror/gcc/releases/gcc-15/COPYING.RUNTIME",
    "MinGW-COPYING.txt": "https://raw.githubusercontent.com/mingw-w64/mingw-w64/master/COPYING",
}


def heif_decoder_sources(info: dict) -> dict:
    """Identify the decoder DLLs actually loaded by the installed pi-heif wheel."""
    heif_version = str(info.get("libheif", ""))
    de265_description = info.get("decoders", {}).get("libde265", "")
    de265_match = re.fullmatch(r"libde265 HEVC decoder, version (\d+\.\d+\.\d+)", de265_description)
    if not re.fullmatch(r"\d+\.\d+\.\d+", heif_version) or de265_match is None:
        raise RuntimeError("Cannot identify pi-heif's native decoder versions for source attribution")
    return {
        name: {
            "version": version,
            "source": f"https://github.com/strukturag/{name}/tree/v{version}",
            "license_source": f"https://raw.githubusercontent.com/strukturag/{name}/v{version}/COPYING",
        }
        for name, version in (("libheif", heif_version), ("libde265", de265_match.group(1)))
    }


def distribution_notice(item: dict) -> str:
    return (f"- {item['name']} {item['version']}; original notices in "
            f"{item['license_directory']}/.")


def copy_exiftool_notices(destination: Path, runtime: Path | None = None, pin: dict | None = None) -> dict:
    pin = pin if pin is not None else fetch_exiftool.load_pin()
    runtime = runtime if runtime is not None else ROOT / 'vendor/exiftool'
    fetch_exiftool.validate(runtime, pin)
    target = destination / 'ExifTool'
    target.mkdir(parents=True, exist_ok=True)
    for name in pin['license_files']:
        original = runtime / fetch_exiftool.relative_name(name)
        copied = target / Path(name).name
        shutil.copyfile(original, copied)
        if fetch_exiftool.sha256(copied) != pin['runtime_files'][name]['sha256']:
            raise ValueError('Copied ExifTool license differs from its original')
    record = fetch_exiftool.provenance(pin)
    (target / 'PROVENANCE.json').write_text(json.dumps(record, indent=2, ensure_ascii=False) + '\n',
                                           encoding='utf8', newline='\n')
    return record


def gather(destination: Path) -> dict:
    import pi_heif

    heif_info = pi_heif.libheif_info()
    decoder_sources = heif_decoder_sources(heif_info)
    upstream = dict(UPSTREAM)
    texts = dict(TEXTS)
    for name, source in decoder_sources.items():
        upstream[name] = (source["source"], f"LGPL-3.0-or-later; pi-heif decoder library {source['version']}")
        texts[f"{name}-COPYING.txt"] = source["license_source"]
    destination.mkdir(parents=True, exist_ok=True)
    exiftool = copy_exiftool_notices(destination)
    distributions = []
    for name in PACKAGES:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            # Essentials + Addons provide all imported Qt modules. The optional
            # umbrella package also depends on unused WebEngine/PDF components.
            if name == 'PySide6':
                continue
            raise
        copied = []
        license_directory = Path("python-distributions") / name
        for entry in dist.files or ():
            rel = Path(str(entry))
            if any("license" in part.lower() for part in rel.parts) or rel.name.lower().startswith(("copying", "notice", "authors")):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    # Retain original relative names below the distribution folder.
                    clean = Path(*[p for p in rel.parts if p not in {"..", "."}])
                    target = destination / license_directory / clean
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(target.relative_to(destination).as_posix())
        distributions.append({
            "name": dist.metadata["Name"], "version": dist.version,
            "declared_license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
            "project_urls": dist.metadata.get_all("Project-URL") or [],
            "license_directory": license_directory.as_posix(),
            "original_license_files": copied,
        })
    upstream_dir = destination / "native-libraries"
    upstream_dir.mkdir(exist_ok=True)
    fetched = []
    failures = []
    for filename, url in texts.items():
        target = upstream_dir / filename
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "LiveViewer-license-collector/0.1"})
            with urllib.request.urlopen(request, timeout=25) as response:
                content = response.read()
            if not content.strip() or content.lstrip().lower().startswith(b"<!doctype html"):
                raise ValueError("Expected a raw original license text")
            target.write_bytes(content)
            fetched.append({"file": str(target.relative_to(destination)), "url": url, "sha256": hashlib.sha256(content).hexdigest()})
        except Exception as error:
            if target.is_file():
                fetched.append({"file": str(target.relative_to(destination)), "url": url, "cached": True})
            else:
                failures.append({"url": url, "error": str(error)})
    # A failed optional upstream URL must be visible in the manifest, while the
    # actual licenses distributed in installed wheels are retained above.
    runtime = {"python": sys.version}
    runtime["qt_ffmpeg_version"] = "9.0.1"
    runtime["heif_info"] = heif_info
    bases = {Path(sysconfig.get_paths()[key]) for key in ("purelib", "platlib")}
    native_files = []
    for base in bases:
        for pattern in ("*.dll", "PySide6/av*.dll", "PySide6/sw*.dll"):
            for path in base.glob(pattern):
                if path.is_file():
                    native_files.append({"file": str(path.relative_to(base)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {
        "application": "LiveViewer 1.0", "distributions": distributions,
        "runtime_versions": runtime, "native_binaries": sorted(native_files, key=lambda x: x["file"]),
        "upstream_sources": [{"component": n, "source": value[0], "license_note": value[1]} for n, value in upstream.items()],
        "pi_heif_decoder_sources": decoder_sources,
        "original_upstream_texts": fetched, "upstream_fetch_failures": failures,
        "external_tools": json.loads((destination / 'FFmpeg-CLI/PROVENANCE.json').read_text(encoding='utf-8')) if (destination / 'FFmpeg-CLI/PROVENANCE.json').is_file() else None,
        "heic_encoder": json.loads((destination / 'HEIC/provenance.json').read_text(encoding='utf-8')) if (destination / 'HEIC/provenance.json').is_file() else None,
        "exiftool": exiftool,
        "redistribution_note": "Qt, FFmpeg, libheif and libde265 remain separate replaceable LGPL shared libraries in this onedir distribution. The frame-reading FFmpeg/ffprobe subprocess tools and their matching DLLs are isolated from Qt under tools/ffmpeg. Keep original notices and corresponding source references with redistributed binaries. PyAV is a development-only fixture generator and is not shipped in the application.",
    }
    (destination / "DEPENDENCIES.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8", newline="\n")
    rows = ["# Third-party components", "", manifest["redistribution_note"], "", "Installed versions and original Python distribution licenses:", ""]
    rows.extend(distribution_notice(item) for item in distributions)
    rows += ["", "Native library license families and corresponding upstream source:", ""]
    rows.extend(f"- {name}: {value[1]}. Source: {value[0]}" for name, value in upstream.items())
    rows += ["", "pi-heif's original wheel notices are preserved verbatim. The decoder source links above use the native library versions reported by this installed wheel; its bundled notice may refer to older releases. The separate HEIC encoder has its own source pins and provenance in HEIC/."]
    rows += ["", "ExifTool 13.38 and its complete Perl runtime are isolated under tools/exiftool. ExifTool/ retains the original Windows distribution license, bundled Strawberry Perl license archive, launcher notice, and the exact accepted runtime file hashes. Restore the fixed archive using scripts/fetch_exiftool.py; an installed application or PATH is never a product dependency."]
    rows += ["", "Qt uses its own dynamically linked FFmpeg build. Exact frame extraction uses separately pinned FFmpeg/ffprobe executables with their own replaceable libraries; FFmpeg-CLI/ records the checksum, upstream license, build configuration and source references. The development-only PyAV installation is excluded from the application.", "", "No runtime component needs to be installed globally. Do not delete or merge these replaceable shared libraries."]
    if failures:
        rows += ["", "Some additional upstream license fetches failed; see DEPENDENCIES.json. Installed wheel license files are preserved unchanged."]
    (destination / "THIRD_PARTY_NOTICES.md").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "licenses")
    args = parser.parse_args()
    manifest = gather(args.output)
    print(f"Retained license files for {len(manifest['distributions'])} distributions and {len(manifest['native_binaries'])} native libraries.")
    if manifest["upstream_fetch_failures"]:
        print(json.dumps(manifest["upstream_fetch_failures"], indent=2))


if __name__ == "__main__":
    main()
