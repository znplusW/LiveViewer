import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def app_directory() -> Path:
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def user_data_directory() -> Path:
    """The local-user data root, independently of the settings portability flag."""
    override = os.environ.get('LIVEGALLERY_DATA_DIR')
    if override:
        return Path(override)
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'LiveViewer'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/LiveViewer'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'LiveViewer'


def cache_data_roots() -> tuple[Path, Path]:
    """Return user/program roots; an explicit test/runtime override isolates both."""
    user = user_data_directory()
    override = os.environ.get('LIVEGALLERY_DATA_DIR')
    program = Path(override) / 'program-data' if override else app_directory() / 'data'
    return user, program


def default_cache_location() -> str:
    return 'program' if (app_directory() / 'portable.flag').exists() else 'user'


def data_directory() -> Path:
    override = os.environ.get('LIVEGALLERY_DATA_DIR')
    if override:
        return Path(override)
    if (app_directory() / 'portable.flag').exists():
        return app_directory() / 'data'
    return user_data_directory()


@dataclass
class Preferences:
    loop: bool = False
    autoplay: bool = False
    muted: bool = True
    preserve_zoom: bool = True
    hold_ms: int = 300
    live_key: str = 'Space'
    live_once_key: str = 'P'
    filmstrip_key: str = 'F'
    filmstrip_visible: bool = True
    horizontal_scroll_mode: str = 'navigate'
    cache_mb: int = 2048
    cache_location: str = 'user'
    recursive: bool = True
    last_folder: str = ''
    last_folders: list[str] = field(default_factory=list)
    search_scope: str = 'current'
    selected_folders: list[str] | None = None
    excluded_folders: list[str] = field(default_factory=list)
    last_image: str = ''
    last_export_directory: str = ''
    sort_by: str = 'name'
    overview_thumbnail_width: int = 204
    filmstrip_height: int = 200
    filmstrip_width: int = 220
    filmstrip_orientation: str = 'horizontal'
    filmstrip_side: str = 'right'
    theme_mode: str = 'dark'

    def normalize_shortcuts(self):
        """Keep an older hold binding when introducing the separate once key."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeySequence

        def sequence(value):
            candidate = QKeySequence(value) if isinstance(value, str) else QKeySequence()
            if (candidate.isEmpty() or candidate.count() != 1
                    or candidate[0].key() in (Qt.Key.Key_unknown, Qt.Key.Key_Escape)):
                return QKeySequence()
            return candidate

        hold = sequence(self.live_key)
        if hold.isEmpty():
            hold = QKeySequence('Space')
        once = sequence(self.live_once_key)
        if once.isEmpty() or once == hold:
            once = next(QKeySequence(key) for key in ('P', 'F9', 'Return') if QKeySequence(key) != hold)
        self.live_key = hold.toString(QKeySequence.SequenceFormat.PortableText)
        self.live_once_key = once.toString(QKeySequence.SequenceFormat.PortableText)
        filmstrip = sequence(self.filmstrip_key)
        if filmstrip.isEmpty() or filmstrip in (hold, once):
            filmstrip = next(QKeySequence(key) for key in ('F', 'F10', 'F8', 'Ctrl+F')
                             if QKeySequence(key) not in (hold, once))
        self.filmstrip_key = filmstrip.toString(QKeySequence.SequenceFormat.PortableText)

    @classmethod
    def load(cls):
        try:
            values = json.loads((data_directory() / 'settings.json').read_text(encoding='utf-8'))
            valid = {f.name for f in fields(cls)}
            result = cls(**{k: v for k, v in values.items() if k in valid})
            if values.get('cache_location') not in ('user', 'program', 'library'):
                result.cache_location = default_cache_location()
            if not isinstance(result.last_folder, str) or '\0' in result.last_folder:
                result.last_folder = ''
            remembered = result.last_folders if isinstance(result.last_folders, list) else []
            result.last_folders = list(dict.fromkeys(value for value in remembered
                                                    if isinstance(value, str) and value and '\0' not in value))
            if 'last_folders' not in values and result.last_folder:
                result.last_folders = [result.last_folder]
            if result.last_folders:
                result.last_folder = result.last_folders[0]
            else:
                result.last_folder = ''
            excluded = result.excluded_folders if isinstance(result.excluded_folders, list) else []
            result.excluded_folders = list(dict.fromkeys(value for value in excluded
                                                        if isinstance(value, str) and value and '\0' not in value))
            if result.search_scope not in ('all', 'current'):
                result.search_scope = 'current'
            if result.selected_folders is not None:
                selected = result.selected_folders
                if not isinstance(selected, list):
                    result.selected_folders = None
                else:
                    valid_paths = list(dict.fromkeys(value for value in selected
                                                     if isinstance(value, str) and value and '\0' not in value))
                    result.selected_folders = valid_paths if valid_paths or not selected else None
            if not isinstance(result.last_export_directory, str) or '\0' in result.last_export_directory:
                result.last_export_directory = ''
            if result.theme_mode not in ('dark', 'light', 'system'):
                result.theme_mode = 'dark'
            result.hold_ms = max(150, min(1000, int(result.hold_ms)))
            result.cache_mb = max(256, min(16384, int(result.cache_mb)))
            try:
                if isinstance(result.overview_thumbnail_width, bool):
                    raise ValueError('not a width')
                result.overview_thumbnail_width = max(120, min(480, int(result.overview_thumbnail_width)))
            except (ValueError, TypeError):
                result.overview_thumbnail_width = 204
            try:
                if isinstance(result.filmstrip_height, bool):
                    raise ValueError('not a height')
                result.filmstrip_height = max(160, min(480, int(result.filmstrip_height)))
            except (ValueError, TypeError):
                result.filmstrip_height = 200
            try:
                if isinstance(result.filmstrip_width, bool):
                    raise ValueError('not a width')
                result.filmstrip_width = max(180, min(480, int(result.filmstrip_width)))
            except (ValueError, TypeError):
                result.filmstrip_width = 220
            if result.filmstrip_orientation not in ('horizontal', 'vertical'):
                result.filmstrip_orientation = 'horizontal'
            if result.filmstrip_side not in ('left', 'right'):
                result.filmstrip_side = 'right'
            result.normalize_shortcuts()
            if not isinstance(result.filmstrip_visible, bool):
                result.filmstrip_visible = True
            if not isinstance(result.autoplay, bool):
                result.autoplay = False
            if result.horizontal_scroll_mode not in ('navigate', 'pan'):
                result.horizontal_scroll_mode = 'navigate'
            return result
        except (OSError, ValueError, TypeError):
            return cls(cache_location=default_cache_location())

    def save(self):
        base = data_directory()
        base.mkdir(parents=True, exist_ok=True)
        target = base / 'settings.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)
