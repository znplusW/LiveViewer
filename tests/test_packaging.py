"""A release archive must never carry a developer's runtime library/cache data."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_windows, verify_bundle


class PackagingDataTests(unittest.TestCase):
    def test_portable_and_source_archives_omit_runtime_data_but_keep_code_and_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / 'dist/LiveViewer'
            program.mkdir(parents=True)
            (program / 'LiveViewer.exe').write_bytes(b'fake executable for archive-only test')
            (program / '_internal').mkdir()
            (program / '_internal/normal.dll').write_bytes(b'packaged component')
            (root / 'docs').mkdir()
            for name in build_windows.USER_DOCUMENTS:
                (root / 'docs' / name).write_text(f'LiveViewer v{build_windows.VERSION}\n', encoding='utf-8')
            (root / 'livegallery').mkdir()
            (root / 'livegallery/data_directory.py').write_text('# source code\n', encoding='utf-8')
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
