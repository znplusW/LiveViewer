"""Run user32 injection before the full Windows build to catch routing failures."""
import json
from pathlib import Path
from PySide6.QtWidgets import QApplication
from livegallery.native_input_selftest import check_native_bezel_input

if __name__ == '__main__':
    app = QApplication([])
    report = {'status': 'failed'}
    try:
        report.update(check_native_bezel_input(), status='passed')
    finally:
        destination = Path('reports/native-bezel-probe.json')
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))
