from drawing_renamer.models import FieldKind, NormalizedRect
from drawing_renamer.ocr_service import AnchorSuggestionService, OcrLine, PaddleOcrService


def line(text: str, x: float, y: float, width: float = 0.08, confidence: float = 0.99) -> OcrLine:
    return OcrLine(text, confidence, NormalizedRect(x, y, width, 0.02))


def test_fields_are_selected_near_company_anchor() -> None:
    service = AnchorSuggestionService(PaddleOcrService())
    anchor = NormalizedRect(0.82, 0.80, 0.16, 0.04)
    lines = [
        line("更改文件号", 0.60, 0.85),
        line("泵体", 0.88, 0.86, 0.04),
        line("CP41.100A", 0.73, 0.85, 0.08),
        line("B.0044.02.017", 0.86, 0.91, 0.11),
    ]
    _boxes, recognized = service._classify_nearby_fields(lines, anchor)
    assert recognized[FieldKind.MATERIAL][0] == "B.0044.02.017"
    assert recognized[FieldKind.PROCESS][0] == "CP41.100A"
    assert recognized[FieldKind.NAME][0] == "泵体"


def test_process_code_has_no_fixed_prefix() -> None:
    service = AnchorSuggestionService(PaddleOcrService())
    assert service._process_score("AB12.30/2") > 0
    assert service._process_score("X9-100A") > 0
