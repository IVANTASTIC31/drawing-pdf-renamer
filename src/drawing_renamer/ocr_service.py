from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

import numpy as np
from PIL import Image

from .models import FieldKind, NormalizedRect


COMPANY_NAME = "湖州三井低温设备有限公司"
COMPANY_MARKERS = ("湖州三井", "三井低温", "低温设备", "有限公司", "SANJING", "CRYOGENIC")


@dataclass(slots=True)
class OcrLine:
    text: str
    confidence: float
    rect: NormalizedRect


@dataclass(slots=True)
class SuggestionResult:
    rotation: int
    boxes: dict[FieldKind, NormalizedRect]
    recognized: dict[FieldKind, tuple[str, float]]
    anchor_found: bool
    message: str


class OcrUnavailableError(RuntimeError):
    pass


class PaddleOcrService:
    """Lazy PaddleOCR adapter; the UI still works when OCR is unavailable."""

    def __init__(self) -> None:
        self._pipeline: Any | None = None

    def _ensure_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        try:
            from paddleocr import PaddleOCR
        except Exception as exc:  # pragma: no cover - depends on optional runtime
            raise OcrUnavailableError(f"OCR组件不可用：{exc}") from exc

        try:
            self._pipeline = PaddleOCR(
                text_detection_model_name="PP-OCRv6_small_det",
                text_recognition_model_name="PP-OCRv6_small_rec",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="cpu",
                enable_mkldnn=False,
                cpu_threads=6,
            )
        except Exception as exc:  # pragma: no cover - model/runtime dependent
            raise OcrUnavailableError(f"OCR初始化失败：{exc}") from exc
        return self._pipeline

    def recognize(self, image: Image.Image) -> list[OcrLine]:
        pipeline = self._ensure_pipeline()
        try:
            result = pipeline.predict(np.asarray(image.convert("RGB")))
            lines: list[OcrLine] = []
            for page_result in result:
                lines.extend(self._parse_result(page_result, image.width, image.height))
            return lines
        except OcrUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover - model/runtime dependent
            raise OcrUnavailableError(f"OCR识别失败：{exc}") from exc

    def recognize_text(self, image: Image.Image) -> tuple[str, float | None]:
        lines = self.recognize(image)
        if not lines:
            return "", None
        lines.sort(key=lambda line: (line.rect.y, line.rect.x))
        text = "".join(line.text.strip() for line in lines if line.text.strip())
        scores = [line.confidence for line in lines if line.text.strip()]
        return text, sum(scores) / len(scores) if scores else None

    @staticmethod
    def _parse_result(result: Any, width: int, height: int) -> list[OcrLine]:
        payload: Any = getattr(result, "json", result)
        if callable(payload):
            payload = payload()
        if isinstance(payload, str):
            payload = json.loads(payload)
        if hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        if not isinstance(payload, dict):
            try:
                payload = dict(payload)
            except (TypeError, ValueError):
                return []

        data = payload.get("res", payload)
        texts = list(data.get("rec_texts", []))
        scores = list(data.get("rec_scores", []))
        boxes = data.get("rec_boxes")
        polygons = data.get("rec_polys")

        lines: list[OcrLine] = []
        for index, text in enumerate(texts):
            text = str(text).strip()
            if not text:
                continue
            score = float(scores[index]) if index < len(scores) else 0.0
            coords: list[float]
            if boxes is not None and index < len(boxes):
                coords = [float(value) for value in boxes[index]]
                x0, y0, x1, y1 = coords[:4]
            elif polygons is not None and index < len(polygons):
                points = list(polygons[index])
                xs = [float(point[0]) for point in points]
                ys = [float(point[1]) for point in points]
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            else:
                continue
            rect = NormalizedRect(x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height)
            lines.append(OcrLine(text, score, rect.clamped()))
        return lines


class PaddleTextRecognitionService:
    """Recognition-only backend for a tightly cropped, single text line."""

    def __init__(self) -> None:
        self._model: Any | None = None

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from paddleocr import TextRecognition
        except Exception as exc:  # pragma: no cover - optional native runtime
            raise OcrUnavailableError(f"文字识别组件不可用：{exc}") from exc
        try:
            self._model = TextRecognition(
                model_name="PP-OCRv6_small_rec",
                device="cpu",
            )
        except Exception as exc:  # pragma: no cover - model/runtime dependent
            raise OcrUnavailableError(f"文字识别模型初始化失败：{exc}") from exc
        return self._model

    def recognize_text(self, image: Image.Image) -> tuple[str, float | None]:
        model = self._ensure_model()
        rgb = image.convert("RGB")
        if rgb.height < 32:
            scale = 32 / max(rgb.height, 1)
            rgb = rgb.resize((max(1, round(rgb.width * scale)), 32), Image.Resampling.LANCZOS)
        try:
            outputs = model.predict(input=np.asarray(rgb), batch_size=1)
            for output in outputs:
                payload: Any = getattr(output, "json", output)
                if callable(payload):
                    payload = payload()
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if hasattr(payload, "to_dict"):
                    payload = payload.to_dict()
                if not isinstance(payload, dict):
                    continue
                data = payload.get("res", payload)
                text = str(data.get("rec_text", "")).strip()
                score = data.get("rec_score")
                return text, float(score) if score is not None else None
            return "", None
        except Exception as exc:  # pragma: no cover - model/runtime dependent
            raise OcrUnavailableError(f"文字识别失败：{exc}") from exc


class AnchorSuggestionService:
    def __init__(self, ocr: PaddleOcrService) -> None:
        self.ocr = ocr

    def suggest(self, image: Image.Image) -> SuggestionResult:
        best: tuple[float, int, Image.Image, list[OcrLine], NormalizedRect | None] | None = None
        for rotation in (0, 90, 270, 180):
            rotated = image if rotation == 0 else image.rotate(rotation, expand=True, fillcolor="white")
            lines = self.ocr.recognize(rotated)
            text_score, anchor = self._find_company_anchor(lines)
            aspect_ratio = 0.0
            if anchor and anchor.height > 0:
                aspect_ratio = (anchor.width * rotated.width) / (anchor.height * rotated.height)
            orientation_score = min(1.0, aspect_ratio / 5.0)
            score = text_score * 0.78 + orientation_score * 0.22
            candidate = (score, rotation, rotated, lines, anchor)
            if best is None or score > best[0]:
                best = candidate
            if text_score >= 0.92 and aspect_ratio >= 3.0:
                break

        if best is None:
            return SuggestionResult(0, {}, {}, False, "未获得OCR结果，请手工框选并输入")

        score, rotation, _rotated, lines, anchor = best
        if anchor is None:
            return SuggestionResult(rotation, {}, {}, False, "未找到公司名称，请手工框选三个区域")

        boxes, recognized = self._classify_nearby_fields(lines, anchor)
        self._fill_geometric_fallbacks(boxes, anchor)
        found_count = len(recognized)
        message = f"已按公司名称生成建议框，并识别出 {found_count}/3 个字段；请逐项核对"
        return SuggestionResult(rotation, boxes, recognized, True, message)

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\s·.,，。:：_\-]", "", value).upper()

    def _find_company_anchor(self, lines: list[OcrLine]) -> tuple[float, NormalizedRect | None]:
        target = self._normalize(COMPANY_NAME)
        matches: list[tuple[float, OcrLine]] = []
        for line in lines:
            normalized = self._normalize(line.text)
            similarity = SequenceMatcher(None, normalized, target).ratio()
            marker_hit = any(self._normalize(marker) in normalized for marker in COMPANY_MARKERS)
            if marker_hit:
                similarity = max(similarity, 0.75)
            if similarity >= 0.35:
                matches.append((similarity * max(line.confidence, 0.5), line))

        if not matches:
            return 0.0, None
        matches.sort(key=lambda item: item[0], reverse=True)
        score, main = matches[0]
        related = [
            line
            for _, line in matches
            if abs(line.rect.y - main.rect.y) < 0.08 and abs(line.rect.x - main.rect.x) < 0.35
        ]
        return min(1.0, score + 0.08 * (len(related) - 1)), self._union(line.rect for line in related)

    def _classify_nearby_fields(
        self, lines: list[OcrLine], anchor: NormalizedRect
    ) -> tuple[dict[FieldKind, NormalizedRect], dict[FieldKind, tuple[str, float]]]:
        nearby = [line for line in lines if self._near_anchor(line.rect, anchor)]
        boxes: dict[FieldKind, NormalizedRect] = {}
        recognized: dict[FieldKind, tuple[str, float]] = {}

        material = self._best(nearby, anchor, self._material_score)
        if material:
            self._put(FieldKind.MATERIAL, material, boxes, recognized)

        process = self._best(
            [line for line in nearby if line is not material], anchor, self._process_score
        )
        if process:
            self._put(FieldKind.PROCESS, process, boxes, recognized)

        excluded = {id(line) for line in (material, process) if line is not None}
        below_company = [
            line
            for line in nearby
            if id(line) not in excluded
            and line.rect.y >= anchor.y + anchor.height * 0.35
            and line.rect.y <= anchor.y + 0.18
            and line.rect.x + line.rect.width / 2 >= anchor.x - 0.06
            and line.rect.x + line.rect.width / 2 <= anchor.x + anchor.width + 0.06
        ]
        name = self._best(
            below_company, anchor, self._name_score
        )
        if name:
            self._put(FieldKind.NAME, name, boxes, recognized)
        return boxes, recognized

    @staticmethod
    def _material_score(text: str) -> float:
        compact = re.sub(r"\s+", "", text).replace("．", ".")
        if re.fullmatch(r"[A-Za-z]\.?\d+(?:\.\d+)+", compact):
            return 1.0
        if re.search(r"[A-Za-z]", compact) and len(re.findall(r"\d+", compact)) >= 2 and "." in compact:
            return 0.72
        return 0.0

    @staticmethod
    def _process_score(text: str) -> float:
        compact = re.sub(r"\s+", "", text)
        if not (re.search(r"[A-Za-z]", compact) and re.search(r"\d", compact)):
            return 0.0
        if not re.fullmatch(r"[A-Za-z0-9./\\\-]+", compact):
            return 0.0
        groups = len(re.findall(r"\d+", compact))
        return min(0.95, 0.55 + 0.08 * groups + (0.12 if re.search(r"[./\-]", compact) else 0))

    def _name_score(self, text: str) -> float:
        compact = text.strip()
        if any(
            marker in compact.upper()
            for marker in (
                "有限公司",
                "SANJING",
                "名称",
                "材料",
                "比例",
                "更改文件号",
                "阶段标记",
                "重量",
                "签名",
                "年月日",
            )
        ):
            return 0.0
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", compact))
        if chinese_count == 0 or len(compact) > 24:
            return 0.0
        return min(0.92, 0.5 + chinese_count * 0.07)

    def _best(self, lines: list[OcrLine], anchor: NormalizedRect, scorer: Any) -> OcrLine | None:
        ranked: list[tuple[float, OcrLine]] = []
        anchor_center = (anchor.x + anchor.width / 2, anchor.y + anchor.height / 2)
        for line in lines:
            content_score = float(scorer(line.text))
            if content_score <= 0:
                continue
            center = (line.rect.x + line.rect.width / 2, line.rect.y + line.rect.height / 2)
            distance = ((center[0] - anchor_center[0]) ** 2 + (center[1] - anchor_center[1]) ** 2) ** 0.5
            proximity = max(0.0, 1.0 - distance / 0.55)
            ranked.append((content_score * 0.7 + proximity * 0.2 + line.confidence * 0.1, line))
        return max(ranked, default=(0.0, None), key=lambda item: item[0])[1]

    @staticmethod
    def _put(
        kind: FieldKind,
        line: OcrLine,
        boxes: dict[FieldKind, NormalizedRect],
        recognized: dict[FieldKind, tuple[str, float]],
    ) -> None:
        rect = line.rect
        boxes[kind] = NormalizedRect(
            rect.x - 0.012,
            rect.y - 0.008,
            rect.width + 0.024,
            rect.height + 0.016,
        ).clamped()
        recognized[kind] = (line.text.strip(), line.confidence)

    @staticmethod
    def _near_anchor(rect: NormalizedRect, anchor: NormalizedRect) -> bool:
        x = rect.x + rect.width / 2
        y = rect.y + rect.height / 2
        ax = anchor.x + anchor.width / 2
        ay = anchor.y + anchor.height / 2
        return abs(x - ax) <= 0.48 and abs(y - ay) <= 0.30

    @staticmethod
    def _fill_geometric_fallbacks(boxes: dict[FieldKind, NormalizedRect], anchor: NormalizedRect) -> None:
        base_height = max(anchor.height, 0.025)
        base_width = max(anchor.width, 0.18)
        defaults = {
            FieldKind.NAME: NormalizedRect(anchor.x, anchor.y + base_height * 1.0, base_width, base_height * 1.7),
            FieldKind.MATERIAL: NormalizedRect(anchor.x, anchor.y + base_height * 2.8, base_width, base_height * 1.7),
            FieldKind.PROCESS: NormalizedRect(
                anchor.x - base_width * 0.9,
                anchor.y + base_height * 1.0,
                base_width * 0.85,
                base_height * 1.7,
            ),
        }
        for kind, rect in defaults.items():
            boxes.setdefault(kind, rect.clamped())

    @staticmethod
    def _union(rects: Iterable[NormalizedRect]) -> NormalizedRect:
        values = list(rects)
        x0 = min(rect.x for rect in values)
        y0 = min(rect.y for rect in values)
        x1 = max(rect.x + rect.width for rect in values)
        y1 = max(rect.y + rect.height for rect in values)
        return NormalizedRect(x0, y0, x1 - x0, y1 - y0).clamped()
