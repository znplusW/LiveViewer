"""Build the Windows x64 portable folder and reproducible delivery archives."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "LiveViewer"
VERSION = "1.0"
USER_DOCUMENTS = ("使用说明.txt", "产品说明.txt")
REFERENCE_DOCUMENT = "LivePhoto-07-08-09-方法与代码.html"
ROOT_DOCUMENTS = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")
PUBLIC_DOCS = frozenset((*USER_DOCUMENTS, REFERENCE_DOCUMENT,
    "ui-discussion-2026-10-26.md", "USER_GUIDE.md", "BUILDING.md",
    "RELEASE_NOTES_v1.0.md", "SOURCE_DISTRIBUTION.md", "GITHUB_RELEASE.md"))
# These reviewed files are included while the release work is still uncommitted.
SOURCE_ADDITIONS = frozenset({
    "livegallery/filename_field.py", "livegallery/photo_info_bar.py", "livegallery/sound_button.py",
    "tests/test_filename_ui.py", "tests/test_sound_button.py", "tests/test_ui_layout_v1.py",
    "docs/ui-discussion-2026-10-26.md", "NOTICE", "licenses/LiveViewer-MIT-NOTICE.txt",
    *("docs/" + name for name in PUBLIC_DOCS),
})
SOURCE_ROOT_FILES = frozenset((*ROOT_DOCUMENTS, "README.md", ".gitignore", "LiveViewer.spec",
                              "launch.py", "requirements.txt", "requirements-build.txt"))
RUNTIME_DIRECTORIES = frozenset({"data", "liveviewer cache", ".liveviewer-work"})
SOURCE_EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist",
    "release", "reports", "work", "outputs", "tools", "vendor",
})


def is_runtime_path(relative: Path) -> bool:
    return (any(part.casefold() in RUNTIME_DIRECTORIES for part in relative.parts)
            or relative.name.casefold() == "portable.flag")


def is_pinned_perl_data_file(path: Path, relative: Path) -> bool:
    """Only two original Perl modules use a directory named Data."""
    prefix = '_internal/tools/exiftool/'
    name = relative.as_posix()
    if not name.startswith(prefix):
        return False
    name = name.removeprefix(prefix)
    if name not in {'exiftool_files/lib/Data/Dumper.pm',
                    'exiftool_files/lib/auto/Data/Dumper/Dumper.xs.dll'}:
        return False
    if __package__:
        from . import fetch_exiftool
    else:
        import fetch_exiftool
    try:
        fetch_exiftool.no_reparse(path)
        expected = fetch_exiftool.load_pin()['runtime_files'][name]
        return path.is_file() and path.stat().st_size == expected['bytes'] and fetch_exiftool.sha256(path) == expected['sha256']
    except (OSError, ValueError, KeyError):
        return False


def _safe_source_name(relative: Path) -> bool:
    parts = [part.casefold() for part in relative.parts]
    return bool(parts) and not relative.is_absolute() and ".." not in parts and not any(
        part in SOURCE_EXCLUDED_DIRECTORIES or part in {"agents.md", ".env", "credentials", "credentials.json",
            "credentials.ini", "secrets.json", "secrets.yml", "secrets.yaml", "id_rsa", "id_ed25519"}
        or part.startswith(".env.") or ":" in part
        or (part.startswith(".") and part not in {".github", ".gitignore"})
        for part in parts) and not is_runtime_path(relative) and relative.suffix.casefold() not in {
            ".pyc", ".pyo", ".pem", ".key", ".pfx", ".p12", ".kdbx"}


def _regular_source(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Reparse source path rejected: {item}")
        if item == ROOT:
            break
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Source member is not a regular file: {path}")


def _archive_source_allowed(relative: Path, fixtures: set[str]) -> bool:
    """Explicit source-ZIP rules also work without Git or developer tools."""
    parts, suffix = relative.parts, relative.suffix.casefold()
    if len(parts) == 1:
        return relative.name in SOURCE_ROOT_FILES
    if parts[0] in {"livegallery", "scripts"}:
        return suffix == ".py"
    if parts[0] == "tests":
        return suffix == ".py" or relative.as_posix() in fixtures
    if parts[0] == "docs":
        return len(parts) == 2 and parts[1] in PUBLIC_DOCS
    if parts[0] == "assets":
        return suffix in {".ico", ".png", ".svg"}
    if parts[0] == "dependencies":
        return len(parts) == 2 and suffix == ".json"
    if parts[:2] == (".github", "workflows"):
        return len(parts) == 3 and suffix in {".yml", ".yaml"}
    if parts[:2] == ("native", "heic_encoder"):
        return suffix in {".cpp", ".h", ".hpp", ".txt"} or relative.name in {"LICENSE", "COPYING"}
    if parts[0] == "licenses":
        return (suffix in {".txt", ".md", ".json"}
                or relative.name in {"LICENSE", "COPYING", "AUTHORS", "NOTICE"}
                or relative.as_posix() == "licenses/ExifTool/Licenses_Strawberry_Perl.zip")
    return False


def source_files() -> list[Path]:
    """Use Git ownership, or explicit public source rules for an unpacked ZIP."""
    if (ROOT / ".git").exists():
        result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        candidates = set(result.stdout.decode("utf8").split("\0")) | SOURCE_ADDITIONS
    else:
        # An isolated build or published source ZIP deliberately has no .git.
        fixtures = {"tests/fixtures/manifest.json"}
        manifest = ROOT / "tests/fixtures/manifest.json"
        if manifest.is_file():
            _regular_source(manifest)
            data = json.loads(manifest.read_text(encoding="utf8"))
            if data.get("synthetic") is not True:
                raise ValueError("Only explicitly synthetic fixtures belong in a source archive")
            fixtures.update("tests/fixtures/" + name for name in data.get("images", []) + data.get("heic_images", []))
            fixtures.update("tests/fixtures/" + item["path"] for item in data.get("videos", []))
        candidates = set()
        folders = {".github", "livegallery", "tests", "scripts", "docs", "assets", "dependencies", "native", "licenses"}
        for directory, names, files in os.walk(ROOT, followlinks=False):
            names[:] = [name for name in names if _safe_source_name((Path(directory) / name).relative_to(ROOT))
                        and (Path(directory) != ROOT or name in folders)]
            for name in names:
                child = Path(directory) / name
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError(f"Reparse source directory rejected: {child}")
            for name in files:
                relative = (Path(directory) / name).relative_to(ROOT)
                if _archive_source_allowed(relative, fixtures):
                    candidates.add(relative.as_posix())
    selected = []
    for name in sorted(candidates):
        relative = Path(name)
        if not _safe_source_name(relative):
            continue
        path = ROOT / relative
        if path.exists() or path.is_symlink():
            _regular_source(path)
            selected.append(path)
    return selected


def pyinstaller_environment() -> dict[str, str]:
    """Resolve DLLs from this Python/Qt installation and Windows, not ambient tools."""
    environment = dict(os.environ)
    system_root = Path(os.environ['SystemRoot'])
    paths = [Path(sys.prefix), Path(sys.prefix) / 'Scripts',
             Path(sys.base_prefix), Path(sys.base_prefix) / 'DLLs',
             Path(sys.base_prefix) / 'Scripts', system_root / 'System32', system_root]
    environment['PATH'] = os.pathsep.join(dict.fromkeys(str(path) for path in paths))
    return environment


def build() -> None:
    if sys.platform != "win32":
        raise SystemExit("Windows artifacts must be built on Windows x64.")
    if not (ROOT / 'tools/ffmpeg/ffmpeg.exe').is_file():
        subprocess.run([sys.executable, str(ROOT / 'scripts/fetch_frame_tools.py')], cwd=ROOT, check=True)
    if not (ROOT / 'vendor/heic/LiveViewer.HeicEncoder.exe').is_file():
        subprocess.run([sys.executable, str(ROOT / 'scripts/build_heic_encoder.py')], cwd=ROOT, check=True)
    # Verify all pinned Perl side files; a launcher alone is not a usable tool.
    subprocess.run([sys.executable, str(ROOT / 'scripts/fetch_exiftool.py')], cwd=ROOT, check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/collect_licenses.py")], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(ROOT / "LiveViewer.spec")],
        cwd=ROOT, check=True, env=pyinstaller_environment(),
    )


def package() -> None:
    folder = ROOT / "dist" / NAME
    if not (folder / f"{NAME}.exe").is_file():
        raise SystemExit(f"Missing built executable: {folder / (NAME + '.exe')}")
    documents = [ROOT / "docs" / name for name in (*USER_DOCUMENTS, REFERENCE_DOCUMENT)]
    documents += [ROOT / name for name in ROOT_DOCUMENTS]
    for document in documents:
        if not document.is_file():
            raise SystemExit(f"Missing required user document: {document}")
    # Fail before creating archives if Git selection or a source path is unsafe.
    sources = source_files()
    document_names = {document.name for document in documents}
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    portable = release / f"{NAME}-Windows-x64-v{VERSION}.zip"
    source = release / f"{NAME}-Source-v{VERSION}.zip"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            # Repackaging an existing portable folder must not duplicate or
            # retain outdated top-level user documents.
            relative = path.relative_to(folder)
            if (path.is_file() and relative.as_posix() not in document_names
                    and (not is_runtime_path(relative) or is_pinned_perl_data_file(path, relative))):
                archive.write(path, Path(portable.stem) / relative)
        for document in documents:
            archive.write(document, Path(portable.stem) / document.name)
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sources:
            rel = path.relative_to(ROOT)
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
