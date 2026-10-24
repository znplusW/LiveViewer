"""Build the Windows x64 portable folder and reproducible delivery archives."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "LiveViewer"
VERSION = "0.1.0"


def build() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows artifacts must be built on Windows x64.")
    subprocess.run([sys.executable, str(ROOT / "scripts/collect_licenses.py")], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(ROOT / "LiveViewer.spec")],
        cwd=ROOT, check=True,
    )


def package() -> None:
    folder = ROOT / "dist" / NAME
    if not (folder / f"{NAME}.exe").is_file():
        raise SystemExit(f"Missing built executable: {folder / (NAME + '.exe')}")
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    portable = release / f"{NAME}-Windows-x64-v{VERSION}.zip"
    source = release / f"{NAME}-Source-v{VERSION}.zip"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, Path(NAME) / path.relative_to(folder))
    excluded = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist", "release", "reports", "work"}
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT)
            if path.is_file() and not any(part in excluded for part in rel.parts) and path.suffix not in {".pyc", ".pyo"}:
                archive.write(path, Path(f"{NAME}-Source-v{VERSION}") / rel)
    sums = []
    for path in (portable, source):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        sums.append(f"{digest}  {path.name}")
    (release / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    if (ROOT / "licenses").is_dir():
        shutil.copytree(ROOT / "licenses", release / "licenses", dirs_exist_ok=True)
    print("\n".join(sums))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--build-only", action="store_true")
    mode.add_argument("--archive-only", action="store_true")
    args = parser.parse_args()
    if not args.archive_only:
        build()
    if not args.build_only:
        package()


if __name__ == "__main__":
    main()
