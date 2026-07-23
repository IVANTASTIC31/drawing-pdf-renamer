from pathlib import Path

from PySide6.QtWidgets import QApplication

from drawing_renamer.history_service import HistoryEntry, HistoryService
from drawing_renamer.models import FieldKind
from drawing_renamer.ui.history_dialog import HistoryDialog


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _entry(record_id: str, material: str, name: str, process: str) -> HistoryEntry:
    return HistoryEntry(
        record_id=record_id,
        timestamp=f"2026-07-23T10:00:0{record_id}",
        event_type="确认",
        original_path="",
        file_path=f"{record_id}.pdf",
        proposed_filename=f"{material}_{name}_{process}.pdf",
        rotation=0,
        boxes={},
        fields={
            FieldKind.MATERIAL.value: {"label": "物料编码", "text": material},
            FieldKind.NAME.value: {"label": "名称", "text": name},
            FieldKind.PROCESS.value: {"label": "工序编号", "text": process},
        },
        screenshot_path=Path("missing.png"),
        json_path=Path("missing.json"),
    )


def test_history_dialog_filters_live_and_reports_invalid_regex(tmp_path: Path) -> None:
    app = _application()
    service = HistoryService(tmp_path / "history")
    records = [
        _entry("1", "B.0044.02.017", "泵体", "CP41.100A"),
        _entry("2", "M.0430.006", "外卷筒", "AB12.30/2"),
        _entry("3", "S.0001.02.032", "密封垫", "CP41.002"),
    ]
    service.list_entries = lambda: records  # type: ignore[method-assign]
    dialog = HistoryDialog(service)

    assert dialog.record_list.count() == 3
    dialog.search_edit.setText(r"泵体|AB12\.30/\d")
    app.processEvents()
    assert dialog.record_list.count() == 2
    assert dialog.search_status.text() == "找到 2 / 3 条"

    dialog.search_edit.setText("[")
    app.processEvents()
    assert dialog.record_list.count() == 0
    assert dialog.search_status.text().startswith("正则无效：")
    assert "正则表达式无效" in dialog.preview.text()
    dialog.close()
