from __future__ import annotations

import faulthandler
import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from .ocr_service import AnchorSuggestionService, PaddleOcrService, PaddleTextRecognitionService


def _suggestion_payload(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "rotation": result.rotation,
        "boxes": {kind.value: asdict(rect) for kind, rect in result.boxes.items()},
        "recognized": {
            kind.value: [text, confidence]
            for kind, (text, confidence) in result.recognized.items()
        },
        "anchor_found": result.anchor_found,
        "message": result.message,
    }


def _write_progress(progress_path: Path | None, value: int, total: int, field: str) -> None:
    if progress_path is None:
        return
    with progress_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"value": value, "total": total, "field": field}, ensure_ascii=False)
            + "\n"
        )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) not in (4, 5):
        return 2
    mode, input_name, output_name, crash_name = arguments[:4]
    output_path = Path(output_name)
    crash_path = Path(crash_name)
    progress_path = Path(arguments[4]) if len(arguments) == 5 else None

    with crash_path.open("a", encoding="utf-8") as crash_stream:
        faulthandler.enable(crash_stream, all_threads=True)
        try:
            if mode == "recognize":
                with Image.open(input_name) as opened:
                    image = opened.convert("RGB")
                text, confidence = PaddleTextRecognitionService().recognize_text(image)
                result: dict[str, object] = {"text": text, "confidence": confidence}
            elif mode == "recognize_batch":
                manifest = json.loads(Path(input_name).read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("批量OCR输入格式错误")
                recognizer = PaddleTextRecognitionService()
                values: dict[str, list[object]] = {}
                total = len(manifest)
                for index, (field, image_name) in enumerate(manifest.items(), start=1):
                    _write_progress(progress_path, index - 1, total, field)
                    with Image.open(str(image_name)) as opened:
                        field_image = opened.convert("RGB")
                    text, confidence = recognizer.recognize_text(field_image)
                    values[str(field)] = [text, confidence]
                    _write_progress(progress_path, index, total, field)
                result = {"values": values}
            elif mode == "suggest":
                with Image.open(input_name) as opened:
                    image = opened.convert("RGB")
                result = _suggestion_payload(
                    AnchorSuggestionService(PaddleOcrService()).suggest(image)
                )
            else:
                raise ValueError(f"未知OCR模式：{mode}")
            payload = {"ok": True, "result": result}
        except Exception as exc:
            traceback.print_exc(file=crash_stream)
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
