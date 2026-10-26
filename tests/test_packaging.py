"""A release archive must never carry a developer's runtime library/cache data."""
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from scripts import build_windows, collect_licenses, fetch_exiftool, verify_bundle


def release_documents(root):
    (root / 'docs').mkdir(exist_ok=True)
    documents = {}
    for name in (*build_windows.USER_DOCUMENTS, build_windows.REFERENCE_DOCUMENT):
        documents['docs/' + name] = ('Public document: ' + name + '\n').encode('utf8')
    for name in build_windows.ROOT_DOCUMENTS:
        documents[name] = ('Original license/notice: ' + name + '\r\n').encode('utf8')
    for name, content in documents.items():
        (root / name).write_bytes(content)
    return documents


class ExifToolPackagingTests(unittest.TestCase):
    def fixture(self, root):
        files = {'exiftool.exe': b'isolated launcher',
                 'exiftool_files/perl532.dll': b'isolated Perl runtime',
                 'exiftool_files/lib/Data/Dumper.pm': b'original Perl Data module',
                 'exiftool_files/lib/auto/Data/Dumper/Dumper.xs.dll': b'original Perl Data library',
                 'exiftool_files/lib/Image/ExifTool.pm': b"$VERSION = '13.38';\n",
                 'exiftool_files/LICENSE': b'Original license\r\nPreserve bytes.\r\n',
                 'README.txt': b'Original launcher notice\n'}
        archive = root / 'accepted.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for name, content in files.items():
                bundle.writestr('exiftool-13.38_64/' + name, content)
        pin = {'schema': 'liveviewer.exiftool-windows.v1', 'version': '13.38',
               'archive_name': archive.name, 'archive_root': 'exiftool-13.38_64',
               'archive_sha256': fetch_exiftool.sha256(archive), 'archive_bytes': archive.stat().st_size,
               'runtime_file_count': len(files), 'source_projects': {'ExifTool': 'https://exiftool.org/'},
               'runtime_files': {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                                 for name, data in files.items()},
               'license_files': ['exiftool_files/LICENSE', 'README.txt']}
        manifest = root / 'pin.json'
        manifest.write_text(json.dumps(pin), encoding='utf8')
        return files, archive, pin, manifest

    def test_pinned_restore_retains_all_perl_files_and_original_notice_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files, archive, pin, manifest = self.fixture(root)
            runtime = root / 'bundle/_internal/tools/exiftool'
            with patch.object(fetch_exiftool, 'MANIFEST', manifest):
                fetch_exiftool.fetch(runtime, archive)
            self.assertEqual({p.relative_to(runtime).as_posix(): p.read_bytes()
                              for p in runtime.rglob('*') if p.is_file()}, files)
            record = collect_licenses.copy_exiftool_notices(root / 'bundle/_internal/licenses', runtime, pin)
            self.assertEqual(record, fetch_exiftool.provenance(pin))
            self.assertEqual((root / 'bundle/_internal/licenses/ExifTool/LICENSE').read_bytes(),
                             files['exiftool_files/LICENSE'])
            self.assertEqual(verify_bundle.inspect_exiftool(root / 'bundle', pin), [])

    def test_missing_changed_or_extra_side_file_is_rejected_without_overwriting(self):
        for change in ('missing', 'changed', 'extra'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                files, archive, pin, manifest = self.fixture(root)
                runtime = root / 'runtime'
                with patch.object(fetch_exiftool, 'MANIFEST', manifest):
                    fetch_exiftool.fetch(runtime, archive)
                    module = runtime / 'exiftool_files/lib/Image/ExifTool.pm'
                    if change == 'missing':
                        module.unlink()
                    elif change == 'changed':
                        module.write_bytes(b'X' * len(files['exiftool_files/lib/Image/ExifTool.pm']))
                    else:
                        (runtime / 'unexpected.dll').write_bytes(b'unowned extra file')
                    before = {p.relative_to(runtime).as_posix(): p.read_bytes()
                              for p in runtime.rglob('*') if p.is_file()}
                    with self.assertRaisesRegex(ValueError, 'ExifTool runtime'):
                        fetch_exiftool.fetch(runtime, archive)
                    self.assertEqual(before, {p.relative_to(runtime).as_posix(): p.read_bytes()
                                              for p in runtime.rglob('*') if p.is_file()})

    def test_archive_mismatch_and_unsafe_members_never_publish_a_runtime(self):
        for change in ('digest', '../outside.txt', 'exiftool.exe:stream'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, archive, pin, manifest = self.fixture(root)
                if change == 'digest':
                    archive.write_bytes(b'not the pinned archive')
                else:
                    with zipfile.ZipFile(archive, 'a') as bundle:
                        bundle.writestr('exiftool-13.38_64/' + change, b'unsafe')
                    pin.update(archive_sha256=fetch_exiftool.sha256(archive), archive_bytes=archive.stat().st_size)
                    manifest.write_text(json.dumps(pin), encoding='utf8')
                with patch.object(fetch_exiftool, 'MANIFEST', manifest), self.assertRaises(ValueError):
                    fetch_exiftool.fetch(root / 'runtime', archive)
                self.assertFalse((root / 'runtime').exists())
                self.assertFalse((root / 'outside.txt').exists())

    def test_bundle_rejects_perl_namespace_leak_and_changed_license(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, archive, pin, manifest = self.fixture(root)
            runtime = root / 'bundle/_internal/tools/exiftool'
            with patch.object(fetch_exiftool, 'MANIFEST', manifest):
                fetch_exiftool.fetch(runtime, archive)
            collect_licenses.copy_exiftool_notices(root / 'bundle/_internal/licenses', runtime, pin)
            (root / 'bundle/_internal/perl532.dll').write_bytes(b'leaked private DLL')
            (root / 'bundle/_internal/licenses/ExifTool/LICENSE').write_bytes(b'changed notice')
            errors = verify_bundle.inspect_exiftool(root / 'bundle', pin)
            self.assertEqual(len(errors), 2, errors)
            self.assertTrue(any('escaped its isolated directory' in error for error in errors))
            self.assertTrue(any('notice is missing or changed' in error for error in errors))

    def test_portable_preserves_only_exact_pinned_perl_data_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files, archive, pin, manifest = self.fixture(root)
            bundle = root / 'dist/LiveViewer'
            runtime = bundle / '_internal/tools/exiftool'
            with patch.object(fetch_exiftool, 'MANIFEST', manifest):
                fetch_exiftool.fetch(runtime, archive)
                collect_licenses.copy_exiftool_notices(bundle / '_internal/licenses', runtime, pin)
                (bundle / 'LiveViewer.exe').write_bytes(b'fake executable')
                release_documents(root)
                errors = verify_bundle.inspect(bundle)['errors']
                self.assertFalse(any(error.startswith('Runtime user data') for error in errors), errors)
                private = runtime / 'exiftool_files/lib/Data/private.json'
                private.write_bytes(b'user data must stay excluded')
                settings = bundle / 'data/settings.json'
                settings.parent.mkdir()
                settings.write_bytes(b'user settings')
                with patch.object(build_windows, 'ROOT', root), redirect_stdout(io.StringIO()):
                    build_windows.package()
                output = next((root / 'release').glob('*Windows-x64*.zip'))
                with zipfile.ZipFile(output) as packed:
                    names = packed.namelist()
                    for relative in ('exiftool_files/lib/Data/Dumper.pm',
                                     'exiftool_files/lib/auto/Data/Dumper/Dumper.xs.dll'):
                        name = next(name for name in names if name.endswith('/tools/exiftool/' + relative))
                        self.assertEqual(packed.read(name), files[relative])
                    self.assertFalse(any(name.endswith(('/private.json', '/settings.json')) for name in names))
                protected = runtime / 'exiftool_files/lib/Data/Dumper.pm'
                protected.write_bytes(b'X' * len(files['exiftool_files/lib/Data/Dumper.pm']))
                errors = verify_bundle.inspect(bundle)['errors']
                self.assertTrue(any(error.startswith('Runtime user data') and error.endswith('/Data/Dumper.pm')
                                    for error in errors), errors)
                self.assertTrue(any('runtime file inventory differs' in error for error in errors), errors)


class PackagingDataTests(unittest.TestCase):
    def test_pyinstaller_path_uses_current_runtime_and_system_without_mutating_parent(self):
        ambient = os.pathsep.join(('unrelated-poppler', 'unrelated-ffmpeg', 'unrelated-libheif'))
        with patch.dict(os.environ, {'PATH': ambient, 'SystemRoot': 'system-root', 'KEEP_ME': 'value'}):
            environment = build_windows.pyinstaller_environment()
            paths = environment['PATH'].split(os.pathsep)
            self.assertFalse(any(part in paths for part in ambient.split(os.pathsep)))
            self.assertIn(str(Path(sys.prefix) / 'Scripts'), paths)
            self.assertIn(str(Path(sys.base_prefix) / 'DLLs'), paths)
            self.assertIn(str(Path('system-root') / 'System32'), paths)
            self.assertEqual(environment['KEEP_ME'], 'value')
            self.assertEqual(os.environ['PATH'], ambient)

    def test_media_isolation_accepts_distinct_private_stack_and_rejects_root_leaks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qt_source, folder = root / 'qt-source', root / 'bundle'
            qt = folder / '_internal/PySide6'
            qt_source.mkdir()
            qt.mkdir(parents=True)
            for name in ('avcodec-63.dll', 'avformat-63.dll', 'avutil-61.dll',
                         'swresample-7.dll', 'swscale-10.dll', 'Qt6Core.dll'):
                (qt_source / name).write_bytes(b'original Qt ' + name.encode())
                (qt / name).write_bytes((qt_source / name).read_bytes())
            private = folder / '_internal/tools/ffmpeg'
            private.mkdir(parents=True)
            (private / 'avcodec-63.dll').write_bytes(b'different private FFmpeg')
            self.assertEqual(verify_bundle.inspect_media_isolation(folder, qt_source), [])
            (folder / '_internal/avcodec-63.dll').write_bytes((private / 'avcodec-63.dll').read_bytes())
            (folder / '_internal/heif.dll').write_bytes(b'private HEIC encoder')
            errors = verify_bundle.inspect_media_isolation(folder, qt_source)
            self.assertEqual(len(errors), 2, errors)
            self.assertTrue(all('escaped its isolated directory' in error for error in errors))

    def test_qt_media_file_must_match_original_distribution_even_with_correct_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qt_source, folder = root / 'qt-source', root / 'bundle'
            qt = folder / '_internal/PySide6'
            qt_source.mkdir()
            qt.mkdir(parents=True)
            for name in ('avcodec-63.dll', 'avformat-63.dll', 'avutil-61.dll',
                         'swresample-7.dll', 'swscale-10.dll'):
                (qt_source / name).write_bytes(b'original Qt ' + name.encode())
                (qt / name).write_bytes((qt_source / name).read_bytes())
            (qt / 'avcodec-63.dll').write_bytes(b'private CLI codec under Qt filename')
            errors = verify_bundle.inspect_media_isolation(folder, qt_source)
            self.assertEqual(errors, ['Qt DLL differs from the installed Qt distribution: avcodec-63.dll'])

    def test_portable_and_source_archives_omit_runtime_data_but_keep_code_and_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / 'dist/LiveViewer'
            program.mkdir(parents=True)
            (program / 'LiveViewer.exe').write_bytes(b'fake executable for archive-only test')
            (program / '_internal').mkdir()
            (program / '_internal/normal.dll').write_bytes(b'packaged component')
            release_documents(root)
            (root / 'livegallery').mkdir()
            (root / 'livegallery/data_directory.py').write_text('# source code\n', encoding='utf-8')
            (root / 'outputs/old-demo').mkdir(parents=True)
            (root / 'outputs/old-demo/previous-delivery.zip').write_bytes(b'historical deliverable')
            (root / 'AGENTS.md').write_text('local workspace instructions\n', encoding='utf-8')
            for base in (root, program):
                for relative in ('data/cache/user.jpg', 'LiveViewer Cache/thumbnails/user.jpg',
                                 'nested/liveviewer cache/user.jpg', '.LiveViewer-work/staged.jpg',
                                 'portable.flag'):
                    path = base / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'private runtime state must not be shipped')
            with patch.object(build_windows, 'ROOT', root), redirect_stdout(io.StringIO()):
                build_windows.package()
            for archive_path in (root / 'release').glob('*.zip'):
                with zipfile.ZipFile(archive_path) as archive:
                    names = archive.namelist()
                    self.assertIsNone(archive.testzip())
                    self.assertFalse(any('user.jpg' in name or 'staged.jpg' in name
                                         or name.endswith('/portable.flag') for name in names), names)
                    if 'Windows-x64' in archive_path.name:
                        self.assertTrue(any(name.endswith('/LiveViewer.exe') for name in names))
                        self.assertTrue(any(name.endswith('/_internal/normal.dll') for name in names))
                        for document in build_windows.USER_DOCUMENTS:
                            self.assertEqual(sum(name.endswith('/' + document) for name in names), 1)
                    else:
                        self.assertTrue(any(name.endswith('/livegallery/data_directory.py') for name in names))
                        self.assertFalse(any('/outputs/' in name for name in names), names)
                        self.assertFalse(any(name.endswith('/.git') for name in names), names)
                        self.assertFalse(any(name.endswith('/AGENTS.md') for name in names), names)

    def test_git_selection_excludes_unreviewed_files_and_even_tracked_private_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracked = ['README.md', 'livegallery/known.py', '.env', '.env.production',
                       'credentials.json', 'docs/AGENTS.md', 'work/private.py', 'outputs/photo.jpg']
            for name in [*tracked, '.git', 'livegallery/unreviewed.py', 'docs/BUILDING.md']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', encoding='utf8')
            listing = SimpleNamespace(stdout=('\0'.join(tracked) + '\0').encode('utf8'))
            with patch.object(build_windows, 'ROOT', root), \
                    patch.object(build_windows.subprocess, 'run', return_value=listing):
                names = {p.relative_to(root).as_posix() for p in build_windows.source_files()}
            self.assertEqual(names, {'README.md', 'livegallery/known.py', 'docs/BUILDING.md'})

    def test_source_zip_without_git_keeps_only_public_rules_and_declared_synthetic_media(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = ['launch.py', 'livegallery/app.py', 'docs/BUILDING.md',
                      'licenses/ExifTool/Licenses_Strawberry_Perl.zip',
                      '.github/workflows/build-windows.yml', 'tests/fixtures/generated.jpg']
            private = ['.env.local', 'docs/.env', 'docs/private-notes.txt', 'AGENTS.md',
                       'tests/fixtures/personal.jpg', 'licenses/secrets.json', 'outputs/old.zip']
            for name in public + private:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'synthetic packaging fixture')
            (root / 'tests/fixtures/manifest.json').write_text(json.dumps(
                {'synthetic': True, 'images': ['generated.jpg'], 'videos': [], 'heic_images': []}), encoding='utf8')
            with patch.object(build_windows, 'ROOT', root), \
                    patch.object(build_windows.subprocess, 'run', side_effect=AssertionError('Git must not be needed')):
                names = {p.relative_to(root).as_posix() for p in build_windows.source_files()}
            self.assertEqual(names, set(public) | {'tests/fixtures/manifest.json'})

    def test_portable_root_has_exact_current_license_notices_and_all_three_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / 'dist/LiveViewer'
            program.mkdir(parents=True)
            (program / 'LiveViewer.exe').write_bytes(b'synthetic executable')
            documents = release_documents(root)
            for name in documents:
                (program / Path(name).name).write_bytes(b'old top-level copy must be replaced')
            with patch.object(build_windows, 'ROOT', root), redirect_stdout(io.StringIO()):
                build_windows.package()
            output = next((root / 'release').glob('*Windows-x64*.zip'))
            with zipfile.ZipFile(output) as archive:
                for name, content in documents.items():
                    member = output.stem + '/' + Path(name).name
                    self.assertEqual(archive.namelist().count(member), 1)
                    self.assertEqual(archive.read(member), content)
                self.assertIsNone(archive.testzip())

    def test_missing_required_notice_fails_before_creating_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / 'dist/LiveViewer'
            program.mkdir(parents=True)
            (program / 'LiveViewer.exe').write_bytes(b'synthetic executable')
            release_documents(root)
            (root / 'NOTICE').unlink()
            with patch.object(build_windows, 'ROOT', root), self.assertRaisesRegex(SystemExit, 'NOTICE'):
                build_windows.package()
            self.assertFalse((root / 'release').exists())

    def test_ci_restores_both_cover_dependencies_before_preflights_in_both_modes(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/build-windows.yml').read_text('utf8')
        blocks = workflow.split('      - name: ')[1:]
        positions = {block.splitlines()[0]: index for index, block in enumerate(blocks)}
        first_preflight = min(index for name, index in positions.items() if name.startswith('Preflight '))
        for name in ('Restore the pinned ExifTool distribution',
                     'Build private HEIC encoder from pinned sources',
                     'Verify native HEIC sources and binary provenance'):
            self.assertLess(positions[name], first_preflight)
            self.assertNotIn('inputs.preflight_only', blocks[positions[name]])

    def test_bundle_inspection_reports_runtime_cache_and_portable_settings_leaks(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for relative in ('data/settings.json', 'portable.flag',
                             'LiveViewer Cache/thumbnails/cached.jpg', '.LiveViewer-work/cover.jpg'):
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'runtime')
            errors = verify_bundle.inspect(folder)['errors']
            runtime_errors = [message for message in errors if message.startswith('Runtime user data')]
            self.assertEqual(len(runtime_errors), 4)


if __name__ == '__main__':
    unittest.main()
