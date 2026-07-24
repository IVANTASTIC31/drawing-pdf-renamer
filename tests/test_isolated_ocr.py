import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from PIL import Image

from drawing_renamer.isolated_ocr import IsolatedOcrService, OcrCancelledError
from drawing_renamer.models import FieldKind
from drawing_renamer.ocr_service import OcrUnavailableError
from drawing_renamer.subprocess_visibility import (
    hidden_window_options,
    install_hidden_subprocess_policy,
)


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


def test_batch_recognition_uses_one_child_process_and_returns_all_fields(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    starts = 0

    class FakeBatchProcess:
        pid = 321
        returncode = 0

        def __init__(self, command, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal starts
            starts += 1
            manifest = json.loads(Path(command[4]).read_text(encoding="utf-8"))
            assert set(manifest) == {kind.value for kind in FieldKind}
            Path(command[5]).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "values": {
                                FieldKind.MATERIAL.value: ["B.001", 0.98],
                                FieldKind.NAME.value: ["泵体", 0.97],
                                FieldKind.PROCESS.value: ["CP41.100A", 0.96],
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
            return "", ""

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("drawing_renamer.isolated_ocr.subprocess.Popen", FakeBatchProcess)
    images = {kind: Image.new("RGB", (40, 20), "white") for kind in FieldKind}
    values = IsolatedOcrService().recognize_batch(images)

    assert starts == 1
    assert values[FieldKind.MATERIAL] == ("B.001", 0.98)
    assert values[FieldKind.NAME] == ("泵体", 0.97)
    assert values[FieldKind.PROCESS] == ("CP41.100A", 0.96)


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


def test_frozen_application_relaunches_itself_as_ocr_worker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: list[str] = []

    class FakeFrozenProcess:
        pid = 987
        returncode = 0

        def __init__(self, command, **_kwargs):  # type: ignore[no-untyped-def]
            captured.extend(command)
            Path(command[4]).write_text(
                json.dumps({"ok": True, "result": {"text": "CP41.100A", "confidence": 0.99}}),
                encoding="utf-8",
            )

        def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
            return "", ""

        def poll(self):  # type: ignore[no-untyped-def]
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("drawing_renamer.isolated_ocr.sys.frozen", True, raising=False)
    monkeypatch.setattr("drawing_renamer.isolated_ocr.subprocess.Popen", FakeFrozenProcess)

    text, confidence = IsolatedOcrService().recognize_text(Image.new("RGB", (20, 20), "white"))

    assert captured[1:3] == ["--ocr-worker", "recognize"]
    assert text == "CP41.100A"
    assert confidence == 0.99


@pytest.mark.skipif(os.name != "nt", reason="Windows-only process flags")
def test_windows_hidden_options_use_creation_flag_and_startup_info() -> None:
    options = hidden_window_options()

    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    startupinfo = options["startupinfo"]
    assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(os.name != "nt", reason="Windows-only process policy")
def test_worker_policy_hides_dependency_subprocesses(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, _command, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)

    monkeypatch.setattr("drawing_renamer.subprocess_visibility.subprocess.Popen", FakePopen)
    install_hidden_subprocess_policy()
    subprocess.Popen(["hardware-probe"])  # type: ignore[call-overload]

    assert int(captured["creationflags"]) & subprocess.CREATE_NO_WINDOW
    startupinfo = captured["startupinfo"]
    assert isinstance(startupinfo, subprocess.STARTUPINFO)
    assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startupinfo.wShowWindow == subprocess.SW_HIDE
