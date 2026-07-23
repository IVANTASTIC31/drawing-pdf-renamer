from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from drawing_renamer import ocr_worker
from drawing_renamer.models import FieldKind


def test_batch_worker_loads_recognizer_once_and_reports_progress(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    loads = 0

    class FakeRecognizer:
        def __init__(self) -> None:
            nonlocal loads
            loads += 1

        def recognize_text(self, image: Image.Image) -> tuple[str, float]:
            return f"{image.width}x{image.height}", 0.99

    monkeypatch.setattr(ocr_worker, "PaddleTextRecognitionService", FakeRecognizer)
    manifest: dict[str, str] = {}
    for index, kind in enumerate(FieldKind, start=1):
        image_path = tmp_path / f"{kind.value}.png"
        Image.new("RGB", (index * 10, 20), "white").save(image_path)
        manifest[kind.value] = str(image_path)
    input_path = tmp_path / "inputs.json"
    output_path = tmp_path / "result.json"
    crash_path = tmp_path / "crash.log"
    progress_path = tmp_path / "progress.json"
    input_path.write_text(json.dumps(manifest), encoding="utf-8")

    exit_code = ocr_worker.main(
        [
            "recognize_batch",
            str(input_path),
            str(output_path),
            str(crash_path),
            str(progress_path),
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    progress_lines = [
        json.loads(line)
        for line in progress_path.read_text(encoding="utf-8").splitlines()
    ]
    assert exit_code == 0
    assert loads == 1
    assert payload["ok"] is True
    assert payload["result"]["values"][FieldKind.MATERIAL.value] == ["10x20", 0.99]
    assert progress_lines[-1] == {"value": 3, "total": 3, "field": FieldKind.PROCESS.value}
