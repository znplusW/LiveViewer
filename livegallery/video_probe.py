"""Serial Qt/FFmpeg video-frame probe isolated from the interactive process.

Wire protocol: one JSON request per stdin line, one JSON response per stdout
line. Qt/FFmpeg diagnostic output belongs to stderr. Encoded frames are small
JPEGs; source files are never modified.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink


def _ensure_pipe_streams() -> None:
    """PyInstaller's windowed bootloader can leave Python stdio as None.

    A probe is launched with explicit anonymous pipes. Recover those inherited
    OS handles without allocating a visible console window.
    """
    if sys.platform != "win32" or (sys.stdin is not None and sys.stdout is not None):
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    for attribute, identifier, mode, flags in (("stdin", -10, "r", os.O_RDONLY), ("stdout", -11, "w", os.O_WRONLY), ("stderr", -12, "w", os.O_WRONLY)):
        if getattr(sys, attribute) is not None:
            continue
        handle = kernel32.GetStdHandle(wintypes.DWORD(identifier & 0xFFFFFFFF))
        if not handle or handle == wintypes.HANDLE(-1).value:
            continue
        duplicate = wintypes.HANDLE()
        if not kernel32.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
            continue
        descriptor = msvcrt.open_osfhandle(duplicate.value, flags | os.O_BINARY)
        setattr(sys, attribute, os.fdopen(descriptor, mode, encoding="utf-8", buffering=1))


class _Input(QObject):
    request = Signal(object)
    closed = Signal()


class _Probe(QObject):
    def __init__(self, app: QGuiApplication) -> None:
        super().__init__()
        self.app = app
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.frames: list[str] = []
        self.targets: list[float] = []
        self.target_ms = 0
        self.duration_ms = 0
        self.warming = False
        self.waiting = False
        self.seeking_at = 0.0
        self.eof = False
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoOutput(self.sink)
        self.sink.videoFrameChanged.connect(self._frame)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.errorOccurred.connect(self._error)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(lambda: self._finish("frame probe timed out") if self.current else None)
        self.seek_deadline = QTimer(self)
        self.seek_deadline.setSingleShot(True)
        self.seek_deadline.timeout.connect(self._seek_timeout)

    @Slot(object)
    def enqueue(self, request: object) -> None:
        if not isinstance(request, dict):
            self._write({"id": None, "frames": [], "duration_ms": 0, "error": "request must be a JSON object"})
            return
        self.queue.append(request)
        if self.current is None:
            self._next()

    @Slot()
    def input_closed(self) -> None:
        self.eof = True
        if self.current is None and not self.queue:
            self.app.quit()

    def _next(self) -> None:
        if not self.queue:
            if self.eof:
                self.app.quit()
            return
        self.current = self.queue.pop(0)
        self.frames = []
        self.duration_ms = 0
        try:
            path = Path(self.current["path"]).resolve(strict=True)
            if not path.is_file():
                raise ValueError("video path is not a file")
            values = self.current.get("times", [0.12, 0.5, 0.85])
            if not isinstance(values, list) or not 1 <= len(values) <= 8:
                raise ValueError("times must contain between one and eight normalized times")
            self.targets = [min(0.98, max(0.0, float(value))) for value in values]
        except Exception as error:
            self._finish(str(error))
            return
        self.warming = True
        self.waiting = False
        self.deadline.start(20_000)
        self.player.setSource(QUrl.fromLocalFile(str(path)))

    def _status(self, status: QMediaPlayer.MediaStatus) -> None:
        if not self.current:
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.duration_ms = self.player.duration()
            self.player.play()  # Warm the decoder before requesting exact seeks.
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._finish(self.player.errorString() or "unsupported or invalid video")
        elif status == QMediaPlayer.MediaStatus.EndOfMedia and self.warming:
            self._finish("no video frame was decoded")

    def _error(self, _code: QMediaPlayer.Error, message: str) -> None:
        if self.current:
            self._finish(message or "video decoder failed")

    def _seek(self) -> None:
        if not self.current:
            return
        if not self.targets:
            self._finish("")
            return
        self.duration_ms = max(self.duration_ms, self.player.duration())
        normalized = self.targets.pop(0)
        self.target_ms = int(max(0, self.duration_ms - 1) * normalized)
        self.waiting = True
        self.seeking_at = time.monotonic()
        self.player.pause()
        self.player.setPosition(self.target_ms)
        # Paused seeks normally deliver a frame with the FFmpeg backend; resume
        # briefly if the decoder needs another packet to finish the seek.
        self.seek_deadline.start(900)

    def _seek_timeout(self) -> None:
        if not self.current or not self.waiting:
            return
        if time.monotonic() - self.seeking_at < 1.5:
            self.player.play()
            self.seek_deadline.start(1300)
        else:
            # A missing sample should not lose already recovered frames.
            self.waiting = False
            self.player.pause()
            QTimer.singleShot(0, self._seek)

    def _frame(self, frame: QVideoFrame) -> None:
        if self.current is None or not frame.isValid():
            return
        if self.warming:
            self.warming = False
            self.player.pause()
            QTimer.singleShot(0, self._seek)
            return
        if not self.waiting:
            return
        timestamp_ms = frame.startTime() // 1000 if frame.startTime() >= 0 else self.player.position()
        elapsed = time.monotonic() - self.seeking_at
        if abs(timestamp_ms - self.target_ms) > 240 and elapsed < 0.85:
            return
        image = frame.toImage()
        if image.isNull():
            return
        image = image.scaled(384, 384, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        image = image.convertToFormat(QImage.Format.Format_RGB888)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        success = image.save(buffer, "JPEG", 78)
        buffer.close()
        if not success:
            return
        self.frames.append(bytes(data.toBase64()).decode("ascii"))
        self.waiting = False
        self.seek_deadline.stop()
        self.player.pause()
        QTimer.singleShot(0, self._seek)

    def _finish(self, error: str) -> None:
        if self.current is None:
            return
        response = {
            "id": self.current.get("id"), "frames": self.frames,
            "duration_ms": self.duration_ms,
            "error": error if error else ("no sample frame decoded" if not self.frames else ""),
        }
        self.current = None
        self.waiting = False
        self.warming = False
        self.deadline.stop()
        self.seek_deadline.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self._write(response)
        QTimer.singleShot(0, self._next)

    @staticmethod
    def _write(response: dict) -> None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def run_probe_server() -> int:
    _ensure_pipe_streams()
    if sys.stdin is None or sys.stdout is None:
        return 2
    app = QGuiApplication.instance() or QGuiApplication(["LiveViewer-media-probe"])
    probe = _Probe(app)
    channel = _Input()
    channel.request.connect(probe.enqueue)
    channel.closed.connect(probe.input_closed)

    def read_input() -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except Exception as error:
                request = {"id": None, "path": "", "parse_error": str(error)}
            channel.request.emit(request)
        channel.closed.emit()

    thread = threading.Thread(target=read_input, name="probe-json-input", daemon=True)
    thread.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run_probe_server())
