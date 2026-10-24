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
import shutil
import sys
import sysconfig
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6", "Pillow", "pi-heif", "numpy")
UPSTREAM = {
    "Python": ("https://github.com/python/cpython", "PSF-2.0 and included notices"),
    "Qt-PySide6": ("https://download.qt.io/archive/qt/6.12/6.12.0/single/", "LGPL-3.0 and third-party notices"),
    "FFmpeg": ("https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz", "Qt build LGPL-2.1-or-later and included permissive third-party notices"),
    "libheif": ("https://github.com/strukturag/libheif", "LGPL-3.0-or-later"),
    "libde265": ("https://github.com/strukturag/libde265", "LGPL-3.0-or-later"),
    "MinGW-runtime": ("https://www.mingw-w64.org/", "GCC runtime exception, MIT and BSD; see upstream notices"),
}
TEXTS = {
    "Python-LICENSE.txt": f"https://raw.githubusercontent.com/python/cpython/v{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}/LICENSE",
    "LGPL-3.0.txt": "https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/LGPL-3.0-only.txt",
    "GPL-3.0.txt": "https://raw.githubusercontent.com/qt/qtbase/v6.12.0/LICENSES/GPL-3.0-only.txt",
    "FFmpeg-LGPL-2.1.txt": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/COPYING.LGPLv2.1",
    "FFmpeg-GPL-2.0.txt": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/COPYING.GPLv2",
    "FFmpeg-LICENSE.md": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.1/LICENSE.md",
    "libheif-COPYING.txt": "https://raw.githubusercontent.com/strukturag/libheif/v1.23.0/COPYING",
    "libde265-COPYING.txt": "https://raw.githubusercontent.com/strukturag/libde265/v1.1.0/COPYING",
    "GCC-runtime-exception.txt": "https://raw.githubusercontent.com/gcc-mirror/gcc/releases/gcc-15/COPYING.RUNTIME",
    "MinGW-COPYING.txt": "https://raw.githubusercontent.com/mingw-w64/mingw-w64/master/COPYING",
}


def gather(destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
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
        for entry in dist.files or ():
            rel = Path(str(entry))
            if any("license" in part.lower() for part in rel.parts) or rel.name.lower().startswith(("copying", "notice", "authors")):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    # Retain original relative names below the distribution folder.
                    clean = Path(*[p for p in rel.parts if p not in {"..", "."}])
                    target = destination / "python-distributions" / name / clean
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(str(target.relative_to(destination)))
        distributions.append({
            "name": dist.metadata["Name"], "version": dist.version,
            "declared_license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
            "project_urls": dist.metadata.get_all("Project-URL") or [],
            "original_license_files": copied,
        })
    upstream_dir = destination / "native-libraries"
    upstream_dir.mkdir(exist_ok=True)
    fetched = []
    failures = []
    for filename, url in TEXTS.items():
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
    import pi_heif
    runtime["qt_ffmpeg_version"] = "9.0.1"
    runtime["heif_info"] = pi_heif.libheif_info() if hasattr(pi_heif, "libheif_info") else str(getattr(pi_heif, "__version__", "unknown"))
    bases = {Path(sysconfig.get_paths()[key]) for key in ("purelib", "platlib")}
    native_files = []
    for base in bases:
        for pattern in ("*.dll", "PySide6/av*.dll", "PySide6/sw*.dll"):
            for path in base.glob(pattern):
                if path.is_file():
                    native_files.append({"file": str(path.relative_to(base)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {
        "application": "LiveViewer 0.1.0", "distributions": distributions,
        "runtime_versions": runtime, "native_binaries": sorted(native_files, key=lambda x: x["file"]),
        "upstream_sources": [{"component": n, "source": value[0], "license_note": value[1]} for n, value in UPSTREAM.items()],
        "original_upstream_texts": fetched, "upstream_fetch_failures": failures,
        "redistribution_note": "Qt, FFmpeg, libheif and libde265 remain separate replaceable LGPL shared libraries in this onedir distribution. Keep original notices and corresponding source references with redistributed binaries. PyAV is a development-only fixture generator and is not shipped in the application.",
    }
    (destination / "DEPENDENCIES.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    rows = ["# Third-party components", "", manifest["redistribution_note"], "", "Installed versions and original Python distribution licenses:", ""]
    rows.extend(f"- {item['name']} {item['version']}; original notices in python-distributions/{item['name']}/." for item in distributions)
    rows += ["", "Native library license families and corresponding upstream source:", ""]
    rows.extend(f"- {name}: {value[1]}. Source: {value[0]}" for name, value in UPSTREAM.items())
    rows += ["", "Qt uses its own dynamically linked FFmpeg build. The development-only PyAV installation is excluded from the application.", "", "No runtime component needs to be installed globally. Do not delete or merge these replaceable shared libraries."]
    if failures:
        rows += ["", "Some additional upstream license fetches failed; see DEPENDENCIES.json. Installed wheel license files are preserved unchanged."]
    (destination / "THIRD_PARTY_NOTICES.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
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
