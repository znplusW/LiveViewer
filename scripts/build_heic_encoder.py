"""Build an isolated, replaceable LGPL libheif + BSD Kvazaar HEIC encoder.

Only verified release source archives are used; no GPL x265 binary is linked.
The build recipe and source URLs travel with the portable/source distributions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import struct
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'work/heic-build'
PREFIX = WORK / 'install'
DESTINATION = ROOT / 'vendor/heic'
PINS = ROOT / 'dependencies/heic-encoder.json'


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def source_inputs():
    paths = [ROOT / name for name in ('scripts/build_heic_encoder.py',
                                      'dependencies/heic-encoder.json',
                                      'requirements-build.txt', 'requirements.txt')]
    paths.extend(sorted(path for path in (ROOT / 'native/heic_encoder').rglob('*') if path.is_file()))
    if not paths[4:]:
        raise RuntimeError('HEIC helper source files are absent')
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise RuntimeError('HEIC build inputs must be regular source files')
    return {path.relative_to(ROOT).as_posix(): digest(path) for path in paths}


def build_platform():
    return {'platform': sys.platform, 'machine': platform.machine().casefold(),
            'pointer_bits': struct.calcsize('P') * 8,
            'image_os': os.environ.get('ImageOS', ''), 'image_version': os.environ.get('ImageVersion', '')}


def _regular_files(folder):
    if not folder.is_dir() or folder.is_symlink() or getattr(folder, 'is_junction', lambda: False)():
        raise RuntimeError(f'HEIC artifact directory is absent or linked: {folder}')
    paths = list(folder.iterdir())
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise RuntimeError(f'HEIC artifacts must contain regular files only: {folder}')
    return paths


def _verify_amd64(path):
    with path.open('rb') as source:
        if source.read(2) != b'MZ':
            raise RuntimeError(f'HEIC component is not a PE executable: {path.name}')
        source.seek(0x3c)
        offset = source.read(4)
        if len(offset) != 4 or not 64 <= struct.unpack('<I', offset)[0] <= 1024 ** 2:
            raise RuntimeError(f'HEIC component has an invalid PE header: {path.name}')
        source.seek(struct.unpack('<I', offset)[0])
        if source.read(6) != b'PE\0\0\x64\x86':
            raise RuntimeError(f'HEIC component is not Windows AMD64: {path.name}')


def verify():
    """Validate a restored native output against the current build inputs."""
    notices = ROOT / 'licenses/HEIC'
    metadata = notices / 'provenance.json'
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 1024 ** 2:
        raise RuntimeError('HEIC source-build provenance is absent or invalid')
    record = json.loads(metadata.read_text(encoding='utf-8'))
    pins = json.loads(PINS.read_text(encoding='utf-8'))
    if (not isinstance(record, dict) or record.get('sources') != pins
            or record.get('source_inputs') != source_inputs()
            or record.get('build_platform') != build_platform()
            or record.get('recipe') != 'scripts/build_heic_encoder.py'
            or record.get('helper_license') != 'MIT' or record.get('shared_libraries') is not True
            or record.get('x265') is not False or record.get('gpl_encoder') is not False):
        raise RuntimeError('HEIC cache does not match the current pinned sources, recipe or platform')
    files = _regular_files(DESTINATION)
    hashes = {path.name: digest(path) for path in files}
    if not hashes or record.get('binaries') != hashes:
        raise RuntimeError('HEIC cache binary list or SHA-256 differs from its provenance')
    license_hashes = {path.name: digest(path) for path in _regular_files(notices) if path.name != 'provenance.json'}
    if record.get('license_files') != license_hashes or not all(name in license_hashes for name in
            ('Kvazaar-LICENSE.txt', 'libheif-COPYING.txt', 'README.txt')):
        raise RuntimeError('HEIC cache license files differ from their provenance')
    executable = 'LiveViewer.HeicEncoder' + ('.exe' if sys.platform == 'win32' else '')
    if executable not in hashes or any('x265' in name.casefold() for name in hashes):
        raise RuntimeError('HEIC cache has missing or unexpected encoder components')
    if sys.platform == 'win32':
        for pattern in ('*heif*.dll', '*kvazaar*.dll', 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
            if not list(DESTINATION.glob(pattern)):
                raise RuntimeError(f'HEIC cache is missing a required Windows runtime: {pattern}')
        for path in files:
            if path.suffix.casefold() not in ('.exe', '.dll'):
                raise RuntimeError(f'Unexpected HEIC runtime file: {path.name}')
            _verify_amd64(path)
    observed = subprocess.run([str(DESTINATION / executable), '--version'], capture_output=True,
                              text=True, check=True, timeout=30).stdout.strip()
    expected = f'LiveViewer.HeicEncoder libheif {pins["libheif"]["version"]} / Kvazaar'
    if observed != expected or record.get('runtime_version') != observed:
        raise RuntimeError('HEIC helper runtime version differs from the pinned native build')
    return {'status': 'passed', 'source_inputs': len(record['source_inputs']),
            'binaries': len(files), 'license_files': len(license_hashes), 'runtime_version': observed,
            'build_platform': record['build_platform']}


def cmake_command():
    executable = shutil.which('cmake')
    if executable:
        return [executable]
    if importlib.util.find_spec('cmake') is not None:
        return [sys.executable, '-m', 'cmake']
    raise RuntimeError('This source build requires CMake 3.25 or newer.')


def run(*arguments):
    subprocess.run([*cmake_command(), *map(str, arguments)], check=True)


def sources(name, pin):
    cache = ROOT / 'work/dependency-cache'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / pin['url'].rsplit('/', 1)[-1]
    if not archive.exists():
        partial = archive.with_suffix(archive.suffix + '.partial')
        try:
            request = urllib.request.Request(pin['url'], headers={'User-Agent': 'LiveViewer-build'})
            with urllib.request.urlopen(request, timeout=90) as source, partial.open('wb') as target:
                shutil.copyfileobj(source, target)
            if partial.stat().st_size != pin['bytes'] or digest(partial) != pin['sha256']:
                raise ValueError(f'{name} source archive failed size/SHA-256 verification')
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if archive.stat().st_size != pin['bytes'] or digest(archive) != pin['sha256']:
        raise ValueError(f'{name} source archive differs from pinned release')
    source = WORK / f'{name}-{pin["version"]}'
    if not source.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(WORK, filter='data')
    if not (source / 'CMakeLists.txt').is_file():
        raise ValueError(f'{name} archive did not contain the expected source tree')
    return source


def configure_build(source, build, *options):
    arguments = ['-S', source, '-B', build, '-DCMAKE_BUILD_TYPE=Release',
                 f'-DCMAKE_INSTALL_PREFIX={PREFIX}', '-DCMAKE_INSTALL_LIBDIR=lib',
                 '-DBUILD_SHARED_LIBS=ON', *options]
    if sys.platform == 'win32':
        arguments += ['-A', 'x64']
    run(*arguments)
    run('--build', build, '--config', 'Release', '--parallel', min(os.cpu_count() or 2, 8))
    run('--install', build, '--config', 'Release')


def build():
    pins = json.loads(PINS.read_text(encoding='utf-8'))
    WORK.mkdir(parents=True, exist_ok=True)
    kvazaar = sources('kvazaar', pins['kvazaar'])
    libheif = sources('libheif', pins['libheif'])
    configure_build(kvazaar, WORK / 'kvazaar-build', '-DBUILD_TESTS=OFF',
                    '-DBUILD_KVAZAAR_BINARY=OFF', '-DUSE_CRYPTO=OFF',
                    '-DCMAKE_INSTALL_RPATH=@loader_path' if sys.platform == 'darwin'
                    else '-DCMAKE_INSTALL_RPATH=$ORIGIN')
    off = ('LIBDE265', 'X265', 'X264', 'AOM_DECODER', 'AOM_ENCODER', 'DAV1D',
           'SvtEnc', 'RAV1E', 'JPEG_DECODER', 'JPEG_ENCODER', 'OpenJPEG_DECODER',
           'OpenJPEG_ENCODER', 'FFMPEG_DECODER', 'OPENJPH_ENCODER', 'UVG266',
           'VVDEC', 'VVENC', 'WEBCODECS', 'UNCOMPRESSED_CODEC', 'LIBSHARPYUV',
           'HEADER_COMPRESSION', 'EXAMPLES', 'GDK_PIXBUF', 'FUZZERS')
    configure_build(libheif, WORK / 'libheif-build', f'-DCMAKE_PREFIX_PATH={PREFIX}',
                    '-DWITH_KVAZAAR=ON', '-DWITH_KVAZAAR_PLUGIN=OFF',
                    '-DENABLE_PLUGIN_LOADING=OFF', '-DBUILD_DOCUMENTATION=OFF',
                    '-DBUILD_TESTING=OFF', *[f'-DWITH_{name}=OFF' for name in off],
                    '-DCMAKE_INSTALL_RPATH=@loader_path' if sys.platform == 'darwin'
                    else '-DCMAKE_INSTALL_RPATH=$ORIGIN')
    helper = WORK / 'helper-build'
    configure_build(ROOT / 'native/heic_encoder', helper, f'-DCMAKE_PREFIX_PATH={PREFIX}')
    # The folder is exclusively generated by this recipe. Remove previous
    # binaries so no old codec/plugin can survive a dependency change.
    if DESTINATION.exists():
        shutil.rmtree(DESTINATION)
    DESTINATION.mkdir(parents=True, exist_ok=True)
    executable = 'LiveViewer.HeicEncoder' + ('.exe' if sys.platform == 'win32' else '')
    shutil.copy2(PREFIX / executable, DESTINATION / executable)
    pattern = '*.dll' if sys.platform == 'win32' else '*.dylib' if sys.platform == 'darwin' else '*.so*'
    for directory in (PREFIX / 'bin', PREFIX / 'lib'):
        for path in directory.glob(pattern):
            shutil.copy2(path, DESTINATION / path.name, follow_symlinks=True)
    report = subprocess.run([str(DESTINATION / executable), '--version'], capture_output=True,
                            text=True, check=True, timeout=30)
    if f'libheif {pins["libheif"]["version"]}' not in report.stdout:
        raise RuntimeError('The built HEIC helper did not report the expected libheif version')
    notices = ROOT / 'licenses/HEIC'
    notices.mkdir(parents=True, exist_ok=True)
    for source, name in ((kvazaar / 'LICENSE', 'Kvazaar-LICENSE.txt'),
                         (libheif / 'COPYING', 'libheif-COPYING.txt')):
        shutil.copy2(source, notices / name)
    for source in libheif.glob('LICENSE*'):
        if source.is_file():
            shutil.copy2(source, notices / ('libheif-' + source.name))
    provenance = {'sources': pins, 'source_inputs': source_inputs(), 'build_platform': build_platform(),
                  'helper_license': 'MIT', 'shared_libraries': True,
                  'x265': False, 'gpl_encoder': False, 'runtime_version': report.stdout.strip(),
                  'recipe': 'scripts/build_heic_encoder.py',
                  'binaries': {path.name: digest(path) for path in DESTINATION.iterdir() if path.is_file()}}
    (notices / 'README.txt').write_text(
        'HEIC cover export uses libheif (LGPL-3.0-or-later) and Kvazaar (BSD-3-Clause).\n'
        'These shared libraries are replaceable in _internal/tools/heic. No x265 is used.\n'
        'Corresponding source URLs and SHA-256 checksums: provenance.json.\n'
        'Full reproducible build recipe and helper source are in the accompanying source ZIP:\n'
        'scripts/build_heic_encoder.py, dependencies/heic-encoder.json, native/heic_encoder/.\n'
        'You may replace these libraries, rebuild the helper, and debug your modifications.\n', encoding='utf-8')
    provenance['license_files'] = {path.name: digest(path) for path in notices.iterdir()
                                 if path.is_file() and path.name != 'provenance.json'}
    (notices / 'provenance.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(report.stdout.strip())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true', help='Validate existing native output without rebuilding')
    arguments = parser.parse_args()
    if not arguments.verify_only:
        build()
    print(json.dumps(verify(), ensure_ascii=False))
