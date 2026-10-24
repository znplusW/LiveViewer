"""A cached native encoder must match the current inputs and every artifact."""
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import build_heic_encoder as recipe


class NativeCacheVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.native = self.root / 'vendor/heic'
        self.notices = self.root / 'licenses/HEIC'
        self.native.mkdir(parents=True)
        self.notices.mkdir(parents=True)
        for name in ('scripts/build_heic_encoder.py', 'requirements-build.txt', 'requirements.txt',
                     'native/heic_encoder/main.cpp', 'native/heic_encoder/CMakeLists.txt'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('current pinned source input\n')
        pins = self.root / 'dependencies/heic-encoder.json'
        pins.parent.mkdir()
        self.pins = {'libheif': {'version': '1.23.4', 'sha256': 'pinned-source-hash'},
                     'kvazaar': {'version': '2.3.2'}}
        pins.write_text(json.dumps(self.pins))
        for name in ('ROOT', 'DESTINATION', 'PINS'):
            value = {'ROOT': self.root, 'DESTINATION': self.native, 'PINS': pins}[name]
            patcher = patch.object(recipe, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        platform_patch = patch.object(recipe.sys, 'platform', 'win32')
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        machine_patch = patch.object(recipe.platform, 'machine', return_value='AMD64')
        machine_patch.start()
        self.addCleanup(machine_patch.stop)
        image_patch = patch.dict(recipe.os.environ, {'ImageOS': 'win25', 'ImageVersion': 'test-image'})
        image_patch.start()
        self.addCleanup(image_patch.stop)
        binary = bytearray(256)
        binary[:2] = b'MZ'
        struct.pack_into('<I', binary, 0x3c, 0x80)
        binary[0x80:0x86] = b'PE\0\0\x64\x86'
        for name in ('LiveViewer.HeicEncoder.exe', 'heif.dll', 'kvazaar.dll',
                     'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
            (self.native / name).write_bytes(binary)
        for name in ('Kvazaar-LICENSE.txt', 'libheif-COPYING.txt', 'README.txt'):
            (self.notices / name).write_text('native component license\n')
        self.version = 'LiveViewer.HeicEncoder libheif 1.23.4 / Kvazaar'
        self.record = {'sources': self.pins, 'source_inputs': recipe.source_inputs(),
                       'build_platform': recipe.build_platform(), 'recipe': 'scripts/build_heic_encoder.py',
                       'helper_license': 'MIT', 'shared_libraries': True, 'x265': False, 'gpl_encoder': False,
                       'runtime_version': self.version,
                       'binaries': {path.name: recipe.digest(path) for path in self.native.iterdir()},
                       'license_files': {path.name: recipe.digest(path) for path in self.notices.iterdir()}}
        self.write_record()
        process_patch = patch.object(recipe.subprocess, 'run', return_value=SimpleNamespace(stdout=self.version))
        self.process = process_patch.start()
        self.addCleanup(process_patch.stop)

    def write_record(self):
        (self.notices / 'provenance.json').write_text(json.dumps(self.record))

    def test_matching_native_cache_is_verified_without_rebuilding(self):
        with patch.object(recipe, 'run', side_effect=AssertionError('Verification tried to compile')):
            result = recipe.verify()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['binaries'], 6)
        self.assertEqual(result['runtime_version'], self.version)
        self.assertEqual(self.process.call_args.args[0][-1], '--version')

    def test_changed_source_or_preset_is_rejected_before_executing_cached_code(self):
        (self.root / 'native/heic_encoder/CMakeLists.txt').write_text('changed compiler preset')
        with self.assertRaisesRegex(RuntimeError, 'current pinned sources'):
            recipe.verify()
        self.process.assert_not_called()

    def test_wrong_platform_or_pinned_version_is_rejected(self):
        for field, replacement in (('build_platform', {'machine': 'arm64'}), ('sources', {})):
            with self.subTest(field=field):
                original = self.record[field]
                self.record[field] = replacement
                self.write_record()
                with self.assertRaises(RuntimeError):
                    recipe.verify()
                self.record[field] = original

    def test_changed_or_extra_binary_is_rejected(self):
        (self.native / 'heif.dll').write_bytes(b'changed native bytes')
        with self.assertRaisesRegex(RuntimeError, 'binary list or SHA-256'):
            recipe.verify()
        (self.native / 'extra.dll').write_bytes(b'extra')
        with self.assertRaises(RuntimeError):
            recipe.verify()

    def test_x86_binary_with_matching_digest_is_still_rejected(self):
        path = self.native / 'heif.dll'
        contents = bytearray(path.read_bytes())
        struct.pack_into('<H', contents, 0x84, 0x14c)
        path.write_bytes(contents)
        self.record['binaries'][path.name] = recipe.digest(path)
        self.write_record()
        with self.assertRaisesRegex(RuntimeError, 'not Windows AMD64'):
            recipe.verify()

    def test_missing_required_runtime_is_rejected_even_when_manifest_matches(self):
        (self.native / 'vcruntime140_1.dll').unlink()
        del self.record['binaries']['vcruntime140_1.dll']
        self.write_record()
        with self.assertRaisesRegex(RuntimeError, 'missing a required Windows runtime'):
            recipe.verify()

    def test_modified_license_or_wrong_runtime_version_is_rejected(self):
        self.process.return_value.stdout = 'wrong native version'
        with self.assertRaisesRegex(RuntimeError, 'runtime version'):
            recipe.verify()
        self.process.return_value.stdout = self.version
        (self.notices / 'README.txt').write_text('modified provenance notice')
        with self.assertRaisesRegex(RuntimeError, 'license files'):
            recipe.verify()

    def test_unexpected_directory_is_rejected(self):
        (self.native / 'foreign-plugins').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'regular files only'):
            recipe.verify()


if __name__ == '__main__':
    unittest.main()
