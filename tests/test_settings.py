"""Cache placement is independent of settings portability and migrates old users."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from livegallery.settings import Preferences, cache_data_roots, data_directory


class CachePreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.app = self.base / 'application'
        self.app.mkdir()
        self.data = self.base / 'override'
        self.data.mkdir()
        self.environment = patch.dict(os.environ, {'LIVEGALLERY_DATA_DIR': str(self.data)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = patch('livegallery.settings.app_directory', return_value=self.app)
        self.directory.start()
        self.addCleanup(self.directory.stop)

    def test_missing_and_old_preferences_keep_each_original_location(self):
        self.assertEqual(Preferences.load().cache_location, 'user')
        (self.data / 'settings.json').write_text('{"muted": false}', encoding='utf-8')
        self.assertEqual(Preferences.load().cache_location, 'user')
        (self.app / 'portable.flag').touch()
        loaded = Preferences.load()
        self.assertEqual(loaded.cache_location, 'program')
        self.assertFalse(loaded.muted)
        (self.data / 'settings.json').unlink()
        self.assertEqual(Preferences.load().cache_location, 'program')

    def test_explicit_three_modes_survive_settings_portability_changes(self):
        for mode in ('user', 'program', 'library'):
            Preferences(cache_location=mode).save()
            self.assertEqual(Preferences.load().cache_location, mode)
            (self.app / 'portable.flag').touch()
            self.assertEqual(Preferences.load().cache_location, mode)
            (self.app / 'portable.flag').unlink()
        values = json.loads((self.data / 'settings.json').read_text())
        values['cache_location'] = 'unrecognized'
        (self.data / 'settings.json').write_text(json.dumps(values))
        self.assertEqual(Preferences.load().cache_location, 'user')

    def test_override_isolates_both_cache_roots_and_existing_settings_path(self):
        user, program = cache_data_roots()
        self.assertEqual(user, self.data)
        self.assertEqual(program, self.data / 'program-data')
        self.assertEqual(data_directory(), self.data)
        (self.app / 'portable.flag').touch()
        self.assertEqual(data_directory(), self.data)
        self.assertFalse((self.app / 'data').exists())

    def test_overview_and_film_sizes_migrate_clamp_and_keep_other_preferences(self):
        for width, height, expected in ((None, None, (204, 200)),
                                        (-1, 900, (120, 480)),
                                        (900, -1, (480, 160)),
                                        ('bad', True, (204, 200)),
                                        (300, 280, (300, 280))):
            values = {'muted': False}
            if width is not None:
                values.update(overview_thumbnail_width=width, filmstrip_height=height)
            (self.data / 'settings.json').write_text(json.dumps(values), encoding='utf-8')
            loaded = Preferences.load()
            self.assertEqual((loaded.overview_thumbnail_width, loaded.filmstrip_height), expected)
            self.assertFalse(loaded.muted)
        loaded.save()
        self.assertEqual(Preferences.load(), loaded)

    def test_appearance_defaults_roundtrips_and_rejects_invalid_values(self):
        self.assertEqual(Preferences.load().theme_mode, 'dark')
        for mode in ('dark', 'light', 'system'):
            Preferences(theme_mode=mode).save()
            self.assertEqual(Preferences.load().theme_mode, mode)
        for value in ('invalid', None, 7):
            (self.data / 'settings.json').write_text(json.dumps({'theme_mode': value, 'muted': False}))
            loaded = Preferences.load()
            self.assertEqual(loaded.theme_mode, 'dark')
            self.assertFalse(loaded.muted)


if __name__ == '__main__':
    unittest.main()
