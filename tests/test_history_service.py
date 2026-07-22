from __future__ import annotations

from PySide6.QtGui import QColor, QImage

from drawing_renamer.history_service import HistoryService
from drawing_renamer.models import DrawingDocument, FieldKind, NormalizedRect


def test_history_saves_screenshot_and_structured_box_data(tmp_path) -> None:
    source = tmp_path / "KM_001.pdf"
    document = DrawingDocument(source)
    document.proposed_filename = "B.001_泵体_CP41.100A.pdf"
    document.rotation = 90
    document.boxes[FieldKind.MATERIAL] = NormalizedRect(0.1, 0.2, 0.3, 0.1)
    document.fields[FieldKind.MATERIAL].text = "B.001"
    document.fields[FieldKind.MATERIAL].confidence = 0.99
    service = HistoryService(tmp_path / "history")
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))

    entry = service.save(service.create_payload(document, "确认"), image)
    entries = service.list_entries()

    assert entry.screenshot_path.is_file()
    assert entry.json_path.is_file()
    assert len(entries) == 1
    assert entries[0].proposed_filename == document.proposed_filename
    assert entries[0].rotation == 90
    assert entries[0].boxes[FieldKind.MATERIAL.value]["x"] == 0.1
    assert entries[0].fields[FieldKind.MATERIAL.value]["text"] == "B.001"
