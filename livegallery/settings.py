import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def app_directory() -> Path:
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def data_directory() -> Path:
    override = os.environ.get('LIVEGALLERY_DATA_DIR')
    if override:
        return Path(override)
    if (app_directory() / 'portable.flag').exists():
        return app_directory() / 'data'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'LiveViewer'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/LiveViewer'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'LiveViewer'


@dataclass
class Preferences:
    loop: bool = False
    muted: bool = True
    preserve_zoom: bool = True
    hold_ms: int = 300
    live_key: str = 'Space'
    cache_mb: int = 2048
    recursive: bool = True
    last_folder: str = ''
    last_image: str = ''
    sort_by: str = 'name'

    @classmethod
    def load(cls):
        try:
            values = json.loads((data_directory() / 'settings.json').read_text(encoding='utf-8'))
            valid = {f.name for f in fields(cls)}
            result = cls(**{k: v for k, v in values.items() if k in valid})
            result.hold_ms = max(150, min(1000, int(result.hold_ms)))
            result.cache_mb = max(256, min(16384, int(result.cache_mb)))
            return result
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self):
        base = data_directory()
        base.mkdir(parents=True, exist_ok=True)
        target = base / 'settings.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)
