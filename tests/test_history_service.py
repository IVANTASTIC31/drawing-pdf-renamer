from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from drawing_renamer.history_service import HistoryEntry, HistoryService
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


def _entry(record_id: str, material: str, name: str, process: str) -> HistoryEntry:
    return HistoryEntry(
        record_id=record_id,
        timestamp=f"2026-07-23T10:00:0{record_id}",
        event_type="确认",
        original_path="",
        file_path="",
        proposed_filename="",
        rotation=0,
        boxes={},
        fields={
            FieldKind.MATERIAL.value: {"text": material},
            FieldKind.NAME.value: {"text": name},
            FieldKind.PROCESS.value: {"text": process},
        },
        screenshot_path=Path(),
        json_path=Path(),
    )


def test_history_regex_search_matches_any_of_three_fields() -> None:
    entries = [
        _entry("1", "B.0044.02.017", "泵体", "CP41.100A"),
        _entry("2", "M.0430.006", "外卷筒", "AB12.30/2"),
        _entry("3", "S.0001.02.032", "密封垫", "CP41.002"),
    ]

    assert [item.record_id for item in HistoryService.filter_entries(entries, r"B\.0044")] == ["1"]
    assert [item.record_id for item in HistoryService.filter_entries(entries, "泵体|密封垫")] == ["1", "3"]
    assert [item.record_id for item in HistoryService.filter_entries(entries, r"ab12\.30/\d")] == ["2"]
    assert HistoryService.filter_entries(entries, "") == entries


def test_history_regex_search_rejects_invalid_expression() -> None:
    with pytest.raises(re.error):
        HistoryService.filter_entries([], "[")


def test_clear_all_removes_records_and_screenshots_but_keeps_empty_root(tmp_path) -> None:
    source = tmp_path / "KM_001.pdf"
    document = DrawingDocument(source)
    document.proposed_filename = "B.001_泵体_CP41.100A.pdf"
    service = HistoryService(tmp_path / "history")
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    service.save(service.create_payload(document, "确认"), image)
    (service.root / "other.tmp").write_text("temporary", encoding="utf-8")

    deleted_count = service.clear_all()

    assert deleted_count == 1
    assert service.root.is_dir()
    assert list(service.root.iterdir()) == []
    assert service.list_entries() == []
