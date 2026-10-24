"""Fetch one SHA-256-pinned LGPL FFmpeg/ffprobe distribution into a private folder."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'dependencies/ffmpeg-windows.json'


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _machine(path):
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError(f'Not a Windows PE binary: {path.name}')
        stream.seek(0x3c)
        offset = struct.unpack('<I', stream.read(4))[0]
        stream.seek(offset)
        header = stream.read(6)
    if header[:4] != b'PE\0\0' or struct.unpack('<H', header[4:])[0] != 0x8664:
        raise ValueError(f'Expected Windows AMD64 binary: {path.name}')


def validate(destination: Path, pin: dict):
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        _machine(destination / name)
    if sys.platform != 'win32':
        return {'status': 'staged_only', 'reason': 'Runtime inspection requires Windows x64'}
    def output(*arguments):
        process = subprocess.run([str(destination / 'ffmpeg.exe'), *arguments], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, errors='replace', timeout=30,
                                 creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        return process.stdout + process.stderr
    configuration = output('-hide_banner', '-buildconf')
    for forbidden in ('--enable-gpl', '--enable-nonfree', '--enable-libx264', '--enable-libx265'):
        if re.search(r'(?<!\S)' + re.escape(forbidden) + r'(?!\S)', configuration):
            raise ValueError(f'Forbidden FFmpeg configuration: {forbidden}')
    license_text = output('-hide_banner', '-L')
    if 'Lesser General Public License' not in license_text:
        raise ValueError('The pinned frame tools did not identify as LGPL')
    encoders = output('-hide_banner', '-encoders')
    if re.search(r'(?m)^\s*[VAS][A-Z.]{5}\s+libx26[45]\w*\s', encoders):
        raise ValueError('A GPL x264/x265 encoder is present in the frame tools')
    version = output('-version').splitlines()[0]
    if pin['version'] not in version:
        raise ValueError(f'Unexpected frame-tool version: {version}')
    return {'status': 'passed', 'version': version, 'build_configuration': configuration,
            'license': license_text, 'encoders': encoders,
            'gpl_nonfree_x264_x265_disabled': True}


def fetch(destination: Path, archive=None):
    pin = json.loads(MANIFEST.read_text(encoding='utf-8'))
    cache = ROOT / 'work/dependency-cache'
    cache.mkdir(parents=True, exist_ok=True)
    archive = Path(archive) if archive else cache / pin['asset']
    if not archive.is_file() or sha256(archive) != pin['sha256']:
        if archive.exists():
            raise ValueError('Existing FFmpeg archive does not match the pinned SHA-256')
        partial = archive.with_suffix(archive.suffix + '.partial')
        request = urllib.request.Request(pin['url'], headers={'User-Agent': 'LiveViewer-build'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, partial.open('wb') as target:
                shutil.copyfileobj(response, target, 1024 * 1024)
            if partial.stat().st_size != pin['bytes'] or sha256(partial) != pin['sha256']:
                raise ValueError('Downloaded FFmpeg archive failed size/SHA-256 verification')
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if archive.stat().st_size != pin['bytes']:
        raise ValueError('FFmpeg archive size differs from the pinned asset')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='frame-tools-', dir=destination.parent) as temporary:
        stage = Path(temporary)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                parts = PurePosixPath(member.filename).parts
                if member.is_dir():
                    continue
                name = parts[-1]
                in_bin = len(parts) == 3 and parts[1] == 'bin'
                wanted = in_bin and (name in ('ffmpeg.exe', 'ffprobe.exe') or name.endswith('.dll'))
                if wanted or len(parts) == 2 and name == 'LICENSE.txt':
                    with bundle.open(member) as source, (stage / name).open('wb') as target:
                        shutil.copyfileobj(source, target)
        for name in ('ffmpeg.exe', 'ffprobe.exe', 'LICENSE.txt'):
            if not (stage / name).is_file():
                raise ValueError(f'Pinned archive is missing {name}')
        inspection = validate(stage, pin)
        binaries = [{'file': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)}
                    for path in sorted(stage.iterdir()) if path.suffix.lower() in ('.exe', '.dll')]
        provenance = {**pin, 'runtime_validation': inspection, 'binaries': binaries}
        (stage / 'PROVENANCE.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding='utf-8')
        (stage / 'SOURCES.txt').write_text(
            'FFmpeg/ffprobe frame-reading tools (LGPL shared build)\n'
            f"Binary: {pin['url']}\nSHA-256: {pin['sha256']}\n"
            f"Corresponding FFmpeg source: {pin['ffmpeg_source']}\n"
            f"Build scripts, patches and dependency source references: {pin['build_recipe_and_dependency_sources']}\n"
            'These DLLs are replaceable and remain separate from the Qt media libraries.\n'
            'The GPL development-only PyAV/x264/x265 stack is not included.\n', encoding='utf-8')
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(stage, destination)
    licenses = ROOT / 'licenses/FFmpeg-CLI'
    licenses.mkdir(parents=True, exist_ok=True)
    for name in ('LICENSE.txt', 'SOURCES.txt', 'PROVENANCE.json'):
        shutil.copy2(destination / name, licenses / name)
    print(f"Pinned frame tools verified: {pin['version']} ({inspection['status']})")
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=ROOT / 'tools/ffmpeg')
    parser.add_argument('--archive', type=Path)
    arguments = parser.parse_args()
    fetch(arguments.destination, arguments.archive)


if __name__ == '__main__':
    main()
