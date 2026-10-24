"""Desktop and isolated media-worker entry points."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe-server', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--fixtures', type=Path)
    parser.add_argument('--report-dir', type=Path, default=Path('reports'))
    parser.add_argument('--folder', type=Path)
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    if args.probe_server:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from livegallery.video_probe import run_probe_server
        return run_probe_server()
    if args.self_test:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from livegallery.selftest import run_self_test
        return run_self_test(args.fixtures, args.report_dir)
    from livegallery.app import run
    return run(args.folder, args.screenshot)


if __name__ == '__main__':
    raise SystemExit(main())
