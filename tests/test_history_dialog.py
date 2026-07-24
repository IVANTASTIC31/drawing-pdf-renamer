from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

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


def test_history_dialog_highlights_three_recognized_fields(tmp_path: Path) -> None:
    _application()
    service = HistoryService(tmp_path / "history")
    record = _entry("1", "B.0044.02.017", "泵体", "CP41.100A")
    record.fields[FieldKind.MATERIAL.value]["confidence"] = 0.994
    record.fields[FieldKind.NAME.value]["confidence"] = 0.987
    record.fields[FieldKind.PROCESS.value]["manually_edited"] = True
    service.list_entries = lambda: [record]  # type: ignore[method-assign]

    dialog = HistoryDialog(service)
    list_text = dialog.record_list.item(0).text()
    detail_text = dialog.details.toPlainText()
    detail_html = dialog.details.toHtml().lower()

    assert "物料编码：B.0044.02.017" in list_text
    assert "名称：泵体" in list_text
    assert "工序编号：CP41.100A" in list_text
    assert "三项识别结果" in detail_text
    assert "B.0044.02.017" in detail_text
    assert "泵体" in detail_text
    assert "CP41.100A" in detail_text
    assert "OCR置信度：99.4%" in detail_text
    assert "人工修改：是" in detail_text
    assert "#2478ff" in detail_html
    assert "#16a36a" in detail_html
    assert "#f08a24" in detail_html
    dialog.close()


def test_clear_history_requires_confirmation_and_refreshes_dialog(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app = _application()
    service = HistoryService(tmp_path / "history")
    records = [_entry("1", "B.0044.02.017", "泵体", "CP41.100A")]
    service.list_entries = lambda: list(records)  # type: ignore[method-assign]
    clear_calls: list[bool] = []

    def clear_all() -> int:
        clear_calls.append(True)
        records.clear()
        return 1

    service.clear_all = clear_all  # type: ignore[method-assign]
    dialog = HistoryDialog(service)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No,
    )
    dialog.clear_history_button.click()
    assert clear_calls == []
    assert dialog.record_list.count() == 1

    information_messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, text, *_args, **_kwargs: information_messages.append(text),
    )
    dialog.clear_history_button.click()
    app.processEvents()

    assert clear_calls == [True]
    assert dialog.record_list.count() == 0
    assert dialog.search_status.text() == "共 0 条"
    assert information_messages == ["已清除 1 条历史记录。"]
    dialog.close()
