from __future__ import annotations

import threading
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtWidgets import QApplication, QGraphicsItem, QMessageBox, QPushButton

from drawing_renamer.history_service import HistoryService
from drawing_renamer.models import DocumentStatus, DrawingDocument, FieldKind, NormalizedRect
from drawing_renamer.ui.document_view import COLORS, DocumentGraphicsView, RoiItem
from drawing_renamer.ui.main_window import MainWindow


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_roi_label_is_solid_white_text_and_fixed_on_screen() -> None:
    _application()
    item = RoiItem(
        FieldKind.MATERIAL,
        QRectF(100, 100, 300, 80),
        QRectF(0, 0, 1000, 1000),
    )

    assert item.label_background.brush().color() == COLORS[FieldKind.MATERIAL]
    assert item.label_background.pen().style() == Qt.PenStyle.NoPen
    assert item.label_item.brush().color() == Qt.GlobalColor.white
    assert item.label_item.font().pixelSize() == 14
    assert item.label_background.flags() & QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
    assert item.pen().widthF() == 1.0
    assert item.pen().isCosmetic()


def test_background_result_is_polled_on_the_gui_thread() -> None:
    app = _application()
    window = MainWindow()
    received: list[str] = []

    window._run_worker(lambda: "ok", received.append, "test")
    deadline = time.monotonic() + 2
    while not received and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert received == ["ok"]
    assert not window._busy
    window.close()


def test_rotation_and_primary_action_shortcuts_respect_text_editing() -> None:
    app = _application()
    window = MainWindow()
    window.show()
    app.processEvents()

    shortcuts = {shortcut.key().toString(): shortcut for shortcut in window._shortcuts}
    assert {"A", "B", "Space"}.issubset(shortcuts)

    rotations: list[int] = []
    window.rotate_current = rotations.append  # type: ignore[method-assign]
    shortcuts["A"].activated.emit()
    shortcuts["B"].activated.emit()
    assert rotations == [90, -90]

    primary_clicks: list[bool] = []
    window.confirm_button.clicked.disconnect()
    window.confirm_button.clicked.connect(lambda: primary_clicks.append(True))
    window.confirm_button.setEnabled(True)
    shortcuts["Space"].activated.emit()
    assert primary_clicks == [True]

    editor = window.field_edits[FieldKind.NAME]
    editor.setFocus()
    app.processEvents()
    assert editor.hasFocus()

    shortcuts["A"].activated.emit()
    shortcuts["Space"].activated.emit()
    assert rotations == [90, -90]
    assert primary_clicks == [True]

    window.eventFilter(window.instruction, QEvent(QEvent.Type.MouseButtonPress))
    assert not editor.hasFocus()
    shortcuts["Space"].activated.emit()
    assert primary_clicks == [True, True]
    window.close()


def test_zoom_requests_a_capped_visible_high_resolution_region() -> None:
    _application()
    view = DocumentGraphicsView()
    view.resize(500, 400)
    view.set_image(Image.new("RGB", (1000, 800), "white"), preview_dpi=180, max_detail_dpi=300)
    view.resetTransform()
    view.scale(1.5, 1.5)
    requests: list[tuple[NormalizedRect, int]] = []
    view.highResolutionRequested.connect(lambda rect, dpi: requests.append((rect, dpi)))

    view._request_visible_detail()

    assert len(requests) == 1
    rect, dpi = requests[0]
    assert dpi == 270
    assert 0 < rect.width < 1
    assert 0 < rect.height < 1
    view.close()


def test_high_resolution_render_result_is_applied_without_blocking_gui(tmp_path: Path) -> None:
    app = _application()
    window = MainWindow()
    path = tmp_path / "preview-test.pdf"
    path.write_bytes(b"test")
    window.documents = [DrawingDocument(path)]
    window.current_index = 0
    window.preview.set_image(Image.new("RGB", (1000, 800), "white"))
    window.preview.add_box(FieldKind.MATERIAL, NormalizedRect(0.2, 0.3, 0.4, 0.2))
    window.pdf.render_region = lambda *_args, **_kwargs: Image.new("RGB", (300, 200), "blue")  # type: ignore[method-assign]

    window._request_high_resolution(NormalizedRect(0.2, 0.3, 0.4, 0.4), 300)
    deadline = time.monotonic() + 2
    while window.preview.high_res_item is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert window.preview.high_res_item is not None
    assert window.preview.roi_items[FieldKind.MATERIAL].zValue() > window.preview.high_res_item.zValue()
    assert window._preview_future is None
    window.close()


def test_three_boxes_offer_individual_and_combined_recognition() -> None:
    app = _application()
    window = MainWindow()
    path = Path("three-box-preview.pdf")
    document = DrawingDocument(path)
    document.boxes = {
        FieldKind.MATERIAL: NormalizedRect(0.1, 0.1, 0.3, 0.1),
        FieldKind.NAME: NormalizedRect(0.1, 0.3, 0.3, 0.1),
        FieldKind.PROCESS: NormalizedRect(0.1, 0.5, 0.3, 0.1),
    }
    window.documents = [document]
    window.current_index = 0
    image = Image.new("RGB", (1000, 800), "white")
    window.base_images[path] = image
    window.preview.set_image(image, document.boxes)
    window._load_fields(document)
    def recognize_batch(_images, progress_callback=None):  # type: ignore[no-untyped-def]
        values = {
            FieldKind.MATERIAL: ("B.0096.02.036", 0.99),
            FieldKind.NAME: ("冷端", 0.98),
            FieldKind.PROCESS: ("CP41.000 (C02)", 0.97),
        }
        for index, kind in enumerate(FieldKind, start=1):
            if progress_callback is not None:
                progress_callback(index, len(FieldKind), kind.value)
            time.sleep(0.04)
        return values

    window.ocr.recognize_batch = recognize_batch  # type: ignore[method-assign]
    individual_buttons = [button for button in window.findChildren(QPushButton) if button.text() == "仅识别此框"]

    assert len(individual_buttons) == 3
    assert all(button.isEnabled() for button in individual_buttons)
    assert window.recognize_all_button.isEnabled()
    window.recognize_all_button.click()
    deadline = time.monotonic() + 3
    while window._busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert not window._busy
    assert document.fields[FieldKind.MATERIAL].text == "B.0096.02.036"
    assert document.fields[FieldKind.NAME].text == "冷端"
    assert document.fields[FieldKind.PROCESS].text == "CP41.000 (C02)"
    assert window.ocr_progress.value() == 3
    assert window.ocr_progress.format() == "识别完成 3/3"
    window.close()


def test_primary_button_queues_ocr_and_immediately_opens_next_document() -> None:
    app = _application()
    window = MainWindow()
    first_path = Path("first.pdf")
    second_path = Path("second.pdf")
    first = DrawingDocument(first_path)
    second = DrawingDocument(second_path)
    first.boxes = {
        FieldKind.MATERIAL: NormalizedRect(0.1, 0.1, 0.3, 0.1),
        FieldKind.NAME: NormalizedRect(0.1, 0.3, 0.3, 0.1),
        FieldKind.PROCESS: NormalizedRect(0.1, 0.5, 0.3, 0.1),
    }
    window.documents = [first, second]
    window.current_index = 0
    for path in (first_path, second_path):
        window._cache_base_image(path, Image.new("RGB", (1000, 800), "white"))
    window.preview.set_image(window.base_images[first_path], first.boxes)
    window._refresh_list()
    window._load_fields(first)

    def recognize_batch(_images, progress_callback=None):  # type: ignore[no-untyped-def]
        for index, kind in enumerate(FieldKind, start=1):
            if progress_callback is not None:
                progress_callback(index, len(FieldKind), kind.value)
            time.sleep(0.04)
        return {
            FieldKind.MATERIAL: ("B.0044.02.017", 0.99),
            FieldKind.NAME: ("泵体", 0.98),
            FieldKind.PROCESS: ("CP41.100A", 0.97),
        }

    window.ocr.recognize_batch = recognize_batch  # type: ignore[method-assign]

    assert window.confirm_button.text() == "识别并下一份"
    window.confirm_button.click()
    app.processEvents()

    assert window.current_document is second
    assert first.status in (DocumentStatus.OCR_QUEUED, DocumentStatus.OCR_RUNNING)
    assert not first.all_fields_filled

    deadline = time.monotonic() + 3
    while window._background_ocr_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert not window._background_ocr_active
    assert first.status == DocumentStatus.NEEDS_CONFIRMATION
    assert first.fields[FieldKind.MATERIAL].text == "B.0044.02.017"
    assert window.current_document is second
    window.close()


def test_full_page_preview_cache_is_limited_to_three_documents() -> None:
    _application()
    window = MainWindow()
    paths = [Path(f"drawing-{index}.pdf") for index in range(4)]
    for path in paths:
        window._cache_base_image(path, Image.new("RGB", (100, 100), "white"))

    assert len(window.base_images) == 3
    assert paths[0] not in window.base_images
    assert set(window.base_images) == set(paths[1:])
    window.close()


def test_changed_boxes_discard_old_result_but_keep_replacement_job() -> None:
    app = _application()
    window = MainWindow()
    path = Path("revision.pdf")
    document = DrawingDocument(path)
    document.boxes = {
        FieldKind.MATERIAL: NormalizedRect(0.1, 0.1, 0.3, 0.1),
        FieldKind.NAME: NormalizedRect(0.1, 0.3, 0.3, 0.1),
        FieldKind.PROCESS: NormalizedRect(0.1, 0.5, 0.3, 0.1),
    }
    window.documents = [document]
    window.current_index = 0
    image = Image.new("RGB", (1000, 800), "white")
    window._cache_base_image(path, image)
    window.preview.set_image(image, document.boxes)
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def recognize_batch(_images, _progress_callback=None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            release_first.wait(2)
            material = "OLD"
        else:
            material = "B.0044.02.017"
        return {
            FieldKind.MATERIAL: (material, 0.99),
            FieldKind.NAME: ("泵体", 0.98),
            FieldKind.PROCESS: ("CP41.100A", 0.97),
        }

    window.ocr.recognize_batch = recognize_batch  # type: ignore[method-assign]
    assert window._enqueue_background_ocr(document)
    assert first_started.wait(1)

    document.boxes[FieldKind.MATERIAL] = NormalizedRect(0.2, 0.1, 0.3, 0.1)
    document.ocr_revision += 1
    document.status = DocumentStatus.NEEDS_CONFIRMATION
    assert window._enqueue_background_ocr(document)
    release_first.set()

    deadline = time.monotonic() + 3
    while window._background_ocr_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert calls == 2
    assert document.fields[FieldKind.MATERIAL].text == "B.0044.02.017"
    assert document.status == DocumentStatus.NEEDS_CONFIRMATION
    window.close()


def test_individual_recognition_only_updates_its_own_field() -> None:
    app = _application()
    window = MainWindow()
    path = Path("single-box-preview.pdf")
    document = DrawingDocument(path)
    document.status = DocumentStatus.RENAMED
    document.boxes = {
        FieldKind.MATERIAL: NormalizedRect(0.1, 0.1, 0.3, 0.1),
        FieldKind.NAME: NormalizedRect(0.1, 0.3, 0.3, 0.1),
        FieldKind.PROCESS: NormalizedRect(0.1, 0.5, 0.3, 0.1),
    }
    document.fields[FieldKind.MATERIAL].text = "B.0061.02.006"
    document.fields[FieldKind.NAME].text = "端盖"
    document.fields[FieldKind.PROCESS].text = "复合件"
    window.documents = [document]
    window.current_index = 0
    image = Image.new("RGB", (1000, 800), "white")
    window.base_images[path] = image
    window.preview.set_image(image, document.boxes)
    window._load_fields(document)
    calls = 0

    def recognize(_image: Image.Image) -> tuple[str, float]:
        nonlocal calls
        calls += 1
        return "CP41.001", 0.99

    window.ocr.recognize_text = recognize  # type: ignore[method-assign]
    window.recognize_buttons[FieldKind.PROCESS].click()
    deadline = time.monotonic() + 2
    while window._busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert calls == 1
    assert document.fields[FieldKind.MATERIAL].text == "B.0061.02.006"
    assert document.fields[FieldKind.NAME].text == "端盖"
    assert document.fields[FieldKind.PROCESS].text == "CP41.001"
    assert document.status == DocumentStatus.RENAMED
    window.close()


def test_selected_renamed_file_can_be_corrected_and_list_uses_new_name(tmp_path: Path, monkeypatch) -> None:
    _application()
    window = MainWindow()
    window.history = HistoryService(tmp_path / "history")
    source = tmp_path / "B.001_泵体_错误.pdf"
    source.write_bytes(b"pdf")
    document = DrawingDocument(source)
    document.status = DocumentStatus.RENAMED
    document.confirmed_filename = source.name
    document.boxes = {
        FieldKind.MATERIAL: NormalizedRect(0.1, 0.1, 0.3, 0.1),
        FieldKind.NAME: NormalizedRect(0.1, 0.3, 0.3, 0.1),
        FieldKind.PROCESS: NormalizedRect(0.1, 0.5, 0.3, 0.1),
    }
    document.fields[FieldKind.MATERIAL].text = "B.001"
    document.fields[FieldKind.NAME].text = "泵体"
    document.fields[FieldKind.PROCESS].text = "CP41.100A"
    window.documents = [document]
    window.current_index = 0
    image = Image.new("RGB", (1000, 800), "white")
    window.base_images[source] = image
    window.preview.set_image(image, document.boxes)
    window._update_filename(document)
    window._load_fields(document)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok)

    assert window.rename_single_button.isEnabled()
    window.rename_single_button.click()

    expected = tmp_path / "B.001_泵体_CP41.100A.pdf"
    assert document.path == expected
    assert expected.is_file()
    assert not source.exists()
    assert expected in window.base_images
    assert expected.name in window.file_list.item(0).text()
    window.close()
    assert window.history.list_entries()


def test_three_box_ocr_keeps_renamed_file_eligible_for_single_correction(tmp_path: Path) -> None:
    _application()
    window = MainWindow()
    source = tmp_path / "B.0061.02.006_端盖_复合件.pdf"
    source.write_bytes(b"pdf")
    document = DrawingDocument(source)
    document.status = DocumentStatus.RENAMED
    document.confirmed_filename = source.name
    document.boxes = {
        FieldKind.MATERIAL: NormalizedRect(0.1, 0.1, 0.3, 0.1),
        FieldKind.NAME: NormalizedRect(0.1, 0.3, 0.3, 0.1),
        FieldKind.PROCESS: NormalizedRect(0.1, 0.5, 0.3, 0.1),
    }
    window.documents = [document]
    window.current_index = 0

    window._all_recognition_ready(
        (
            source,
            {
                FieldKind.MATERIAL: ("B.0061.02.006", 0.99),
                FieldKind.NAME: ("端盖", 0.99),
                FieldKind.PROCESS: ("CP41.001", 0.99),
            },
        )
    )

    assert document.status == DocumentStatus.RENAMED
    assert document.proposed_filename == "B.0061.02.006_端盖_CP41.001.pdf"
    assert window.rename_single_button.isEnabled()
    assert "已重命名" in window.file_list.item(0).text()
    window.close()


def test_reloaded_or_downgraded_file_can_still_be_renamed_individually(tmp_path: Path, monkeypatch) -> None:
    _application()
    window = MainWindow()
    window.history = HistoryService(tmp_path / "history")
    source = tmp_path / "S.0001.02.032_密封垫_T2-M.pdf"
    source.write_bytes(b"pdf")
    document = DrawingDocument(source)
    document.status = DocumentStatus.NEEDS_CONFIRMATION
    document.fields[FieldKind.MATERIAL].text = "S.0001.02.032"
    document.fields[FieldKind.NAME].text = "密封垫"
    document.fields[FieldKind.PROCESS].text = "CP41.002"
    window.documents = [document]
    window.current_index = 0

    window._update_filename(document)
    window._load_fields(document)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.StandardButton.Ok)

    assert document.proposed_filename == "S.0001.02.032_密封垫_CP41.002.pdf"
    assert window.rename_single_button.isEnabled()
    window.rename_single_button.click()

    expected = tmp_path / "S.0001.02.032_密封垫_CP41.002.pdf"
    assert document.path == expected
    assert expected.is_file()
    assert document.status == DocumentStatus.RENAMED
    window.close()
