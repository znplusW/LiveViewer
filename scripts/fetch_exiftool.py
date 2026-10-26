"""Restore or verify the exact accepted ExifTool runtime without installation.

The pinned archive may be supplied with --archive, or placed in
work/dependency-cache. No current download or external application is searched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'dependencies/exiftool-windows.json'


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative_name(name: str) -> Path:
    path = PurePosixPath(name)
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                *(f'lpt{i}' for i in range(1, 10))}
    if (not name or path.is_absolute() or path.as_posix() != name
            or any(part in ('.', '..') or part.endswith((' ', '.'))
                   or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                   or part.split('.')[0].casefold() in reserved for part in path.parts)):
        raise ValueError(f'Unsafe ExifTool relative path: {name!r}')
    return Path(*path.parts)


def no_reparse(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError(f'ExifTool paths must not be reparse points: {item}')


def load_pin() -> dict:
    pin = json.loads(MANIFEST.read_text(encoding='utf8'))
    if pin.get('schema') != 'liveviewer.exiftool-windows.v1' or pin.get('version') != '13.38':
        raise ValueError('Unexpected ExifTool dependency pin')
    files = pin['runtime_files']
    if len(files) != pin['runtime_file_count'] or len({n.casefold() for n in files}) != len(files):
        raise ValueError('Duplicate or inconsistent ExifTool runtime inventory')
    for name, item in files.items():
        relative_name(name)
        if not re.fullmatch('[0-9a-f]{64}', item['sha256']) or type(item['bytes']) is not int or item['bytes'] < 0:
            raise ValueError('Invalid ExifTool file pin')
    for name in pin['license_files']:
        if name not in files:
            raise ValueError('Missing original ExifTool license pin')
    relative_name(pin['archive_root'])
    relative_name(pin['archive_name'])
    return pin


def validate(folder: Path, pin: dict) -> dict:
    """Require the full runtime, including Perl modules and original licenses."""
    folder = Path(os.path.abspath(folder))
    no_reparse(folder)
    if not folder.is_dir():
        raise ValueError(f'ExifTool runtime is absent: {folder}')
    actual = {}
    for directory, names, files in os.walk(folder, followlinks=False):
        for name in (*names, *files):
            no_reparse(Path(directory) / name)
        for name in files:
            path = Path(directory) / name
            actual[path.relative_to(folder).as_posix()] = path
    expected = pin['runtime_files']
    if actual.keys() != expected.keys():
        raise ValueError('ExifTool runtime file inventory differs from the accepted distribution')
    for name, path in actual.items():
        if path.stat().st_size != expected[name]['bytes'] or sha256(path) != expected[name]['sha256']:
            raise ValueError(f'ExifTool runtime differs from the accepted distribution: {name}')
    return {'status': 'passed', 'version': pin['version'], 'runtime_file_count': len(actual),
            'archive_sha256': pin['archive_sha256']}


def provenance(pin: dict) -> dict:
    return {'schema': 'liveviewer.exiftool-provenance.v1', 'version': pin['version'],
            'archive_sha256': pin['archive_sha256'], 'archive_bytes': pin['archive_bytes'],
            'source_projects': pin['source_projects'], 'runtime_directory': 'tools/exiftool',
            'runtime_files': pin['runtime_files'],
            'license_files': {Path(name).name: pin['runtime_files'][name]['sha256']
                              for name in pin['license_files']}}


def fetch(destination: Path, archive: Path | None = None) -> dict:
    pin = load_pin()
    destination = Path(os.path.abspath(destination))
    no_reparse(destination)
    if destination.exists():
        return validate(destination, pin)  # Never overwrite an existing tree.
    archive = Path(archive) if archive else ROOT / 'work/dependency-cache' / pin['archive_name']
    no_reparse(archive)
    if not archive.is_file():
        raise ValueError('Pinned ExifTool archive is absent; supply --archive or place '
                         + pin['archive_name'] + ' in work/dependency-cache. No application/PATH fallback is allowed.')
    if archive.stat().st_size != pin['archive_bytes'] or sha256(archive) != pin['archive_sha256']:
        raise ValueError('ExifTool archive failed fixed size/SHA-256 verification')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='exiftool-stage-', dir=destination.parent) as temporary:
        stage_root = Path(temporary)
        if not stage_root.absolute().is_relative_to(destination.parent):
            raise ValueError('ExifTool stage escaped the destination parent')
        stage = stage_root / 'runtime'
        stage.mkdir()
        seen = set()
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                member_path = relative_name(member.filename.rstrip('/'))
                parts = member_path.parts
                if not parts or parts[0] != pin['archive_root'] or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError('Unexpected ExifTool archive path or link')
                if member.is_dir():
                    continue
                name = Path(*parts[1:]).as_posix()
                if name not in pin['runtime_files'] or name.casefold() in seen:
                    raise ValueError('Unexpected or duplicate ExifTool archive member')
                seen.add(name.casefold())
                target = stage / relative_name(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
        result = validate(stage, pin)
        if destination.exists():
            raise ValueError('ExifTool destination appeared during preparation')
        stage.rename(destination)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=ROOT / 'vendor/exiftool')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    result = validate(args.destination, load_pin()) if args.verify_only else fetch(args.destination, args.archive)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
