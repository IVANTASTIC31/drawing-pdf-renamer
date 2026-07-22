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


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 4:
        return 2
    mode, input_name, output_name, crash_name = arguments
    output_path = Path(output_name)
    crash_path = Path(crash_name)

    with crash_path.open("a", encoding="utf-8") as crash_stream:
        faulthandler.enable(crash_stream, all_threads=True)
        try:
            with Image.open(input_name) as opened:
                image = opened.convert("RGB")
            if mode == "recognize":
                text, confidence = PaddleTextRecognitionService().recognize_text(image)
                result: dict[str, object] = {"text": text, "confidence": confidence}
            elif mode == "suggest":
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
