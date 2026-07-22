import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from PIL import Image

from drawing_renamer.isolated_ocr import IsolatedOcrService, OcrCancelledError
from drawing_renamer.ocr_service import OcrUnavailableError


def test_recognize_text_reads_child_process_result(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeProcess:
        pid = 123
        returncode = 0

        def __init__(self, command, **_kwargs):  # type: ignore[no-untyped-def]
            Path(command[5]).write_text(
                json.dumps({"ok": True, "result": {"text": "B.001", "confidence": 0.98}}),
                encoding="utf-8",
            )

        def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
            return "", ""

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("drawing_renamer.isolated_ocr.subprocess.Popen", FakeProcess)
    text, confidence = IsolatedOcrService().recognize_text(Image.new("RGB", (20, 20), "white"))
    assert text == "B.001"
    assert confidence == 0.98


def test_native_child_crash_becomes_python_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeCrashProcess:
        pid = 456
        returncode = 3221225477

        def __init__(self, command, **_kwargs):  # type: ignore[no-untyped-def]
            Path(command[6]).write_text("Windows fatal exception: access violation", encoding="utf-8")

        def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
            return "", ""

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("drawing_renamer.isolated_ocr.subprocess.Popen", FakeCrashProcess)
    with pytest.raises(OcrUnavailableError, match="主程序已受到保护"):
        IsolatedOcrService().recognize_text(Image.new("RGB", (20, 20), "white"))


def test_running_worker_can_be_cancelled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started = threading.Event()

    class FakeBlockingProcess:
        pid = 789

        def __init__(self, _command, **_kwargs):  # type: ignore[no-untyped-def]
            self.returncode = None
            self.killed = threading.Event()

        def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
            started.set()
            self.killed.wait(2)
            return "", ""

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9
            self.killed.set()

    monkeypatch.setattr("drawing_renamer.isolated_ocr.subprocess.Popen", FakeBlockingProcess)
    service = IsolatedOcrService()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.recognize_text, Image.new("RGB", (20, 20), "white"))
        assert started.wait(1)
        assert service.cancel_current()
        with pytest.raises(OcrCancelledError):
            future.result(timeout=2)
