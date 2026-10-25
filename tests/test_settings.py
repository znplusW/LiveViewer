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

    def test_last_export_directory_survives_restart_and_rejects_malformed_values(self):
        destination = str(self.base / '导出图片 with spaces')
        Preferences(last_export_directory=destination, muted=False).save()
        loaded = Preferences.load()
        self.assertEqual(loaded.last_export_directory, destination)
        self.assertFalse(loaded.muted)
        for value in (None, True, 7, ['folder'], 'invalid\0path'):
            (self.data / 'settings.json').write_text(json.dumps({'last_export_directory':value, 'muted':False}))
            loaded = Preferences.load()
            self.assertEqual(loaded.last_export_directory, '')
            self.assertFalse(loaded.muted)

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

    def test_multiple_folders_and_search_scope_roundtrip_and_migrate_single_folder(self):
        folders = [str(self.base / '图库 A'), str(self.base / '图库 B')]
        Preferences(last_folders=folders, last_folder=folders[0], search_scope='all', muted=False).save()
        loaded = Preferences.load()
        self.assertEqual(loaded.last_folders, folders)
        self.assertEqual(loaded.last_folder, folders[0])
        self.assertEqual(loaded.search_scope, 'all')
        self.assertFalse(loaded.muted)
        (self.data / 'settings.json').write_text(json.dumps({'last_folder':folders[1]}), encoding='utf-8')
        migrated = Preferences.load()
        self.assertEqual(migrated.last_folders, [folders[1]])
        self.assertEqual(migrated.search_scope, 'current')
        for invalid in (None, True, 'all folders', 7, ['all']):
            (self.data / 'settings.json').write_text(json.dumps({
                'last_folders': [folders[0], None, 7, folders[0], 'bad\0path', folders[1]],
                'search_scope': invalid, 'last_export_directory': str(self.base / 'exports')}), encoding='utf-8')
            normalized = Preferences.load()
            self.assertEqual(normalized.last_folders, folders)
            self.assertEqual(normalized.last_folder, folders[0])
            self.assertEqual(normalized.search_scope, 'current')
            self.assertEqual(normalized.last_export_directory, str(self.base / 'exports'))
        first, second = Preferences(), Preferences()
        first.last_folders.append('one')
        self.assertEqual(second.last_folders, [])

    def test_checked_folder_selection_distinguishes_all_none_and_subset(self):
        for selected in (None, [], [str(self.base / 'one')]):
            Preferences(selected_folders=selected).save()
            self.assertEqual(Preferences.load().selected_folders, selected)
        for invalid in (True, 7, 'one folder', [None, 7, 'bad\0path']):
            (self.data / 'settings.json').write_text(json.dumps({'selected_folders':invalid}), encoding='utf-8')
            self.assertIsNone(Preferences.load().selected_folders)

    def test_invalid_legacy_folder_is_not_migrated_into_startup_paths(self):
        for value in (7, None, True, ['folder'], 'bad\0path'):
            (self.data / 'settings.json').write_text(json.dumps({'last_folder':value}), encoding='utf-8')
            loaded = Preferences.load()
            self.assertEqual(loaded.last_folder, '')
            self.assertEqual(loaded.last_folders, [])

    def test_explicit_empty_import_list_does_not_revive_legacy_root_and_exclusions_are_validated(self):
        values = {'last_folder': str(self.base / 'old'), 'last_folders': [],
                  'excluded_folders': [str(self.base / 'removed'), None, 7, 'bad\0path', str(self.base / 'removed')]}
        (self.data / 'settings.json').write_text(json.dumps(values), encoding='utf-8')
        result = Preferences.load()
        self.assertEqual(result.last_folders, [])
        self.assertEqual(result.last_folder, '')
        self.assertEqual(result.excluded_folders, [str(self.base / 'removed')])
        result.save()
        self.assertEqual(Preferences.load().excluded_folders, result.excluded_folders)

    def test_filmstrip_layout_dimensions_roundtrip_and_validate_without_losing_other_settings(self):
        Preferences(filmstrip_orientation='vertical', filmstrip_side='left', filmstrip_width=310,
                    filmstrip_height=260, muted=False).save()
        result = Preferences.load()
        self.assertEqual((result.filmstrip_orientation, result.filmstrip_side, result.filmstrip_width,
                          result.filmstrip_height, result.muted), ('vertical', 'left', 310, 260, False))
        for width, expected in ((True, 220), ('bad', 220), (20, 180), (900, 480)):
            (self.data / 'settings.json').write_text(json.dumps({'filmstrip_width': width,
                'filmstrip_orientation': 'bad', 'filmstrip_side': 'bad', 'muted': False}), encoding='utf-8')
            result = Preferences.load()
            self.assertEqual((result.filmstrip_width, result.filmstrip_orientation, result.filmstrip_side,
                              result.muted), (expected, 'horizontal', 'right', False))


if __name__ == '__main__':
    unittest.main()
