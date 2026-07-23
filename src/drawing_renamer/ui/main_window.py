from __future__ import annotations

import logging
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any

from PIL import Image
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..diagnostics import DiagnosticsContext, app_data_directory
from ..file_discovery import discover_pdfs, is_pdf_file
from ..history_service import HistoryService
from ..isolated_ocr import IsolatedOcrService, OcrCancelledError
from ..models import DocumentStatus, DrawingDocument, FieldKind, NormalizedRect
from ..naming import build_filename_from_fields, validate_destination
from ..ocr_service import SuggestionResult
from ..pdf_service import PdfService
from ..rename_service import RenameService
from .document_view import COLORS, DocumentGraphicsView
from .history_dialog import HistoryDialog
from .log_dialog import LogDialog


logger = logging.getLogger("drawing_renamer.ui")


@dataclass(frozen=True, slots=True)
class PreviewRenderRequest:
    serial: int
    path: Path
    rotation: int
    rect: NormalizedRect
    dpi: int


@dataclass(slots=True)
class OcrBatchJob:
    document: DrawingDocument
    revision: int
    crops: dict[FieldKind, Image.Image]


class MainWindow(QMainWindow):
    MAX_CACHED_PAGES = 3

    def __init__(self, diagnostics: DiagnosticsContext | None = None) -> None:
        super().__init__()
        self.diagnostics = diagnostics
        self.setWindowTitle("工程图纸 PDF 半自动重命名")
        self.resize(1500, 920)

        self.documents: list[DrawingDocument] = []
        self.current_index = -1
        self.base_images: dict[Path, Image.Image] = {}
        self.pdf = PdfService()
        self.ocr = IsolatedOcrService()
        self.renamer = RenameService()
        history_root = (diagnostics.data_directory if diagnostics else app_data_directory()) / "history"
        self.history = HistoryService(history_root)
        self.history_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-writer")
        self._history_futures: set[Future[Any]] = set()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-controller")
        self._worker_future: Future[Any] | None = None
        self._worker_callback: Callable[[Any], None] | None = None
        self._worker_timer = QTimer(self)
        self._worker_timer.setInterval(80)
        self._worker_timer.timeout.connect(self._poll_worker)
        self._worker_progress_queue: SimpleQueue[tuple[int, int, str]] = SimpleQueue()
        self._worker_progress_total = 0
        self.ocr_queue_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-queue")
        self._ocr_jobs: deque[OcrBatchJob] = deque()
        self._active_ocr_job: OcrBatchJob | None = None
        self._ocr_queue_future: Future[dict[FieldKind, tuple[str, float | None]]] | None = None
        self._ocr_queue_progress: SimpleQueue[tuple[OcrBatchJob, int, int, str]] = SimpleQueue()
        self._ocr_queue_timer = QTimer(self)
        self._ocr_queue_timer.setInterval(80)
        self._ocr_queue_timer.timeout.connect(self._poll_ocr_queue)
        self.preview_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-preview")
        self._preview_future: Future[tuple[PreviewRenderRequest, Image.Image]] | None = None
        self._pending_preview_request: PreviewRenderRequest | None = None
        self._preview_request_serial = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(50)
        self._preview_timer.timeout.connect(self._poll_preview_render)
        self._busy = False
        self._pending_close = False
        self._loading_fields = False

        self.field_edits: dict[FieldKind, QLineEdit] = {}
        self.field_confidence: dict[FieldKind, QLabel] = {}
        self.kind_buttons: dict[FieldKind, QToolButton] = {}
        self.recognize_buttons: dict[FieldKind, QPushButton] = {}
        self.field_cards: dict[FieldKind, QFrame] = {}
        self._build_ui()
        self._bind_shortcuts()
        self._apply_style()
        self._update_action_buttons()

    def _build_ui(self) -> None:
        toolbar = QToolBar("文件与视图", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        open_files = QAction("添加PDF", self)
        open_files.triggered.connect(self.add_files)
        toolbar.addAction(open_files)
        open_folder = QAction("添加文件夹", self)
        open_folder.triggered.connect(self.add_folder)
        toolbar.addAction(open_folder)
        toolbar.addSeparator()

        self.suggest_action = QAction("当前PDF生成建议框", self)
        self.suggest_action.triggered.connect(self.suggest_current)
        toolbar.addAction(self.suggest_action)
        self.auto_suggest_action = QAction("自动建议框：关", self)
        self.auto_suggest_action.setCheckable(True)
        self.auto_suggest_action.setChecked(False)
        self.auto_suggest_action.setToolTip("仅在打开当前PDF时自动查找公司名称；不会批量处理整个文件夹")
        self.auto_suggest_action.toggled.connect(self._auto_suggest_toggled)
        toolbar.addAction(self.auto_suggest_action)
        self.cancel_ocr_action = QAction("取消识别", self)
        self.cancel_ocr_action.setEnabled(False)
        self.cancel_ocr_action.triggered.connect(self.cancel_current_ocr)
        toolbar.addAction(self.cancel_ocr_action)
        toolbar.addSeparator()

        rotate_left = QAction("左旋90°", self)
        rotate_left.triggered.connect(lambda: self.rotate_current(90))
        toolbar.addAction(rotate_left)
        rotate_right = QAction("右旋90°", self)
        rotate_right.triggered.connect(lambda: self.rotate_current(-90))
        toolbar.addAction(rotate_right)
        toolbar.addAction("适合窗口", lambda: self.preview.fit_to_window())
        toolbar.addAction("放大", lambda: self.preview.zoom_in())
        toolbar.addAction("缩小", lambda: self.preview.zoom_out())
        toolbar.addSeparator()
        logs_action = QAction("问题反馈日志", self)
        logs_action.triggered.connect(self.show_log_dialog)
        logs_action.setEnabled(self.diagnostics is not None)
        toolbar.addAction(logs_action)
        help_action = QAction("使用说明", self)
        help_action.triggered.connect(self.show_usage_help)
        toolbar.addAction(help_action)

        root_splitter = QSplitter(Qt.Orientation.Horizontal)
        root_splitter.setChildrenCollapsible(False)
        self.setCentralWidget(root_splitter)

        left = QWidget()
        left.setMinimumWidth(270)
        left.setMaximumWidth(410)
        left_layout = QVBoxLayout(left)
        title_row = QHBoxLayout()
        title = QLabel("文件列表")
        title.setObjectName("sectionTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self.history_button = QPushButton("历史记录")
        self.history_button.setToolTip("查看确认时保存的现场画面、框坐标和OCR结果")
        self.history_button.clicked.connect(self.show_history_dialog)
        title_row.addWidget(self.history_button)
        left_layout.addLayout(title_row)
        self.progress_label = QLabel("尚未添加 PDF")
        self.progress_label.setObjectName("muted")
        left_layout.addWidget(self.progress_label)
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_list.currentRowChanged.connect(self.select_document)
        left_layout.addWidget(self.file_list, 1)
        add_row = QHBoxLayout()
        add_button = QPushButton("添加PDF")
        add_button.clicked.connect(self.add_files)
        folder_button = QPushButton("添加文件夹")
        folder_button.clicked.connect(self.add_folder)
        add_row.addWidget(add_button)
        add_row.addWidget(folder_button)
        left_layout.addLayout(add_row)
        root_splitter.addWidget(left)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.setChildrenCollapsible(False)
        root_splitter.addWidget(right_splitter)

        preview_wrapper = QWidget()
        preview_layout = QVBoxLayout(preview_wrapper)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.instruction = QLabel(
            "手动框选：①点击下方彩色标签　②在图纸文字上按住鼠标左键拖拽　"
            "③松开完成；按 Esc 退出框选后可用左键拖动图纸"
        )
        self.instruction.setObjectName("hint")
        preview_layout.addWidget(self.instruction)
        self.preview = DocumentGraphicsView()
        self.preview.boxesChanged.connect(self._save_current_boxes)
        self.preview.boxFinished.connect(self._box_finished)
        self.preview.highResolutionRequested.connect(self._request_high_resolution)
        self.preview.highResolutionCancelled.connect(self._cancel_high_resolution)
        preview_layout.addWidget(self.preview, 1)
        right_splitter.addWidget(preview_wrapper)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(14, 10, 14, 12)
        detail_title_row = QHBoxLayout()
        detail_title = QLabel("信息确认")
        detail_title.setObjectName("sectionTitle")
        detail_title_row.addWidget(detail_title)
        detail_title_row.addStretch()
        self.ocr_status = QLabel("等待处理")
        self.ocr_status.setObjectName("muted")
        detail_title_row.addWidget(self.ocr_status)
        details_layout.addLayout(detail_title_row)

        fields_row = QHBoxLayout()
        self.kind_group = QButtonGroup(self)
        self.kind_group.setExclusive(True)
        for index, kind in enumerate(FieldKind, start=1):
            card = self._create_field_card(kind, index)
            fields_row.addWidget(card, 1)
        details_layout.addLayout(fields_row)

        recognition_row = QHBoxLayout()
        self.recognize_all_button = QPushButton("一键识别三个框")
        self.recognize_all_button.setObjectName("recognizeAllButton")
        self.recognize_all_button.setEnabled(False)
        self.recognize_all_button.clicked.connect(self.recognize_all_boxes)
        recognition_row.addWidget(self.recognize_all_button)
        self.ocr_progress = QProgressBar()
        self.ocr_progress.setRange(0, len(FieldKind))
        self.ocr_progress.setValue(0)
        self.ocr_progress.setFormat("等待识别 0/3")
        self.ocr_progress.setTextVisible(True)
        recognition_row.addWidget(self.ocr_progress, 1)
        details_layout.addLayout(recognition_row)

        filename_row = QHBoxLayout()
        filename_label = QLabel("新文件名预览")
        filename_label.setObjectName("fieldCaption")
        filename_row.addWidget(filename_label)
        self.filename_preview = QLineEdit()
        self.filename_preview.setReadOnly(True)
        self.filename_preview.setPlaceholderText("三个字段填写完整后生成")
        filename_row.addWidget(self.filename_preview, 1)
        self.confirm_button = QPushButton("确认并下一份")
        self.confirm_button.setObjectName("primaryButton")
        self.confirm_button.clicked.connect(self.confirm_and_next)
        filename_row.addWidget(self.confirm_button)
        self.rename_single_button = QPushButton("重命名选中文件")
        self.rename_single_button.setObjectName("renameSingleButton")
        self.rename_single_button.setEnabled(False)
        self.rename_single_button.clicked.connect(self.rename_selected_file)
        filename_row.addWidget(self.rename_single_button)
        self.execute_button = QPushButton("执行批量重命名")
        self.execute_button.clicked.connect(self.execute_rename)
        filename_row.addWidget(self.execute_button)
        details_layout.addLayout(filename_row)
        right_splitter.addWidget(details)

        root_splitter.setSizes([315, 1185])
        right_splitter.setSizes([650, 250])

        status = QStatusBar(self)
        self.setStatusBar(status)
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setMaximumWidth(180)
        self.busy_bar.hide()
        status.addPermanentWidget(self.busy_bar)
        status.showMessage("请添加单页 PDF")

    def _create_field_card(self, kind: FieldKind, shortcut_number: int) -> QFrame:
        card = QFrame()
        card.setObjectName("fieldCard")
        self.field_cards[kind] = card
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 10)
        header = QHBoxLayout()
        button = QToolButton()
        color_label = {
            FieldKind.MATERIAL: "蓝色框",
            FieldKind.NAME: "绿色框",
            FieldKind.PROCESS: "橙色框",
        }[kind]
        button.setText(f"{shortcut_number}  {kind.label} · {color_label}")
        button.setCheckable(True)
        button.setProperty("kind", kind.value)
        button.setToolTip(f"点击后，在PDF图纸上拖拽添加{color_label}，对应{kind.label}")
        button.clicked.connect(lambda checked, value=kind: self._activate_kind(value if checked else None))
        self.kind_group.addButton(button)
        self.kind_buttons[kind] = button
        header.addWidget(button)
        confidence = QLabel("未识别")
        confidence.setObjectName("muted")
        self.field_confidence[kind] = confidence
        header.addStretch()
        header.addWidget(confidence)
        layout.addLayout(header)

        edit = QLineEdit()
        edit.setPlaceholderText(f"单独识别此框、一键识别三个框或手工输入{kind.label}")
        edit.textEdited.connect(lambda text, value=kind: self._field_edited(value, text))
        self.field_edits[kind] = edit
        layout.addWidget(edit)

        actions = QHBoxLayout()
        recognize = QPushButton("仅识别此框")
        recognize.setEnabled(False)
        recognize.setToolTip(f"只重新识别{kind.label}，保留另外两个字段")
        recognize.clicked.connect(lambda _checked=False, value=kind: self.recognize_box(value))
        self.recognize_buttons[kind] = recognize
        actions.addWidget(recognize)
        clear = QPushButton("清除")
        clear.clicked.connect(lambda _checked=False, value=kind: self.clear_field(value))
        actions.addWidget(clear)
        actions.addStretch()
        layout.addLayout(actions)
        return card

    def _bind_shortcuts(self) -> None:
        for key, kind in zip(("1", "2", "3"), FieldKind):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda value=kind: self._select_kind_button(value))
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.confirm_and_next)
        QShortcut(QKeySequence("R"), self).activated.connect(lambda: self.rotate_current(-90))
        QShortcut(QKeySequence("Delete"), self).activated.connect(self._delete_selected_box)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.exit_box_selection)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f5f7fa; color: #20242b; font-family: "Microsoft YaHei UI"; font-size: 14px; }
            QToolBar { background: white; border-bottom: 1px solid #dfe3e8; spacing: 8px; padding: 5px; }
            QToolBar QToolButton { padding: 7px 10px; }
            QListWidget { background: white; border: 1px solid #dfe3e8; border-radius: 6px; outline: none; }
            QListWidget::item { padding: 10px 8px; border-bottom: 1px solid #edf0f3; }
            QListWidget::item:selected { background: #e8f1ff; color: #155dcc; }
            QLineEdit { background: white; border: 1px solid #ccd3dc; border-radius: 5px; padding: 8px; }
            QLineEdit:focus { border: 2px solid #2478ff; }
            QPushButton, QToolButton { background: white; border: 1px solid #c8d0da; border-radius: 5px; padding: 7px 12px; }
            QPushButton:hover, QToolButton:hover { background: #f0f5fb; }
            QToolButton:checked { background: #e8f1ff; border: 2px solid #2478ff; }
            #primaryButton { background: #2478ff; color: white; border: none; padding: 9px 18px; font-weight: 600; }
            #primaryButton:hover { background: #155fcc; }
            #recognizeAllButton { background: #2f6fd4; color: white; border: none; padding: 8px 18px; font-weight: 700; }
            #recognizeAllButton:hover { background: #245db4; }
            #recognizeAllButton:disabled { background: #aab7c9; color: #eef2f7; }
            #renameSingleButton { background: #d92d20; color: white; border: 1px solid #b42318; font-weight: 700; }
            #renameSingleButton:hover { background: #b42318; border-color: #912018; }
            #renameSingleButton:pressed { background: #8f1d14; border-color: #75160f; }
            #renameSingleButton:disabled { background: #f1f3f5; color: #9aa2ac; border-color: #d7dce2; }
            #sectionTitle { font-size: 19px; font-weight: 700; }
            #fieldCaption { font-weight: 600; }
            #muted { color: #727b86; font-size: 12px; }
            #hint { background: #fff7dc; color: #6c5521; border: 1px solid #ead89c; padding: 7px 12px; }
            #fieldCard { background: white; border: 1px solid #dde2e8; border-radius: 7px; }
            QSplitter::handle { background: #dfe3e8; }
            """
        )
        for kind, button in self.kind_buttons.items():
            color = COLORS[kind].name()
            dark = COLORS[kind].darker(120).name()
            soft = COLORS[kind].lighter(185).name()
            button.setStyleSheet(
                f"""
                QToolButton {{
                    background: {color}; color: white; border: 2px solid {color};
                    border-radius: 5px; padding: 7px 12px; font-weight: 700;
                }}
                QToolButton:hover {{ background: {dark}; border-color: {dark}; }}
                QToolButton:checked {{ background: {dark}; border: 3px solid #20242b; }}
                """
            )
            self.field_cards[kind].setStyleSheet(
                f"""
                QFrame#fieldCard {{
                    background: {soft}; border: 2px solid {color}; border-radius: 7px;
                }}
                """
            )

    @property
    def current_document(self) -> DrawingDocument | None:
        if 0 <= self.current_index < len(self.documents):
            return self.documents[self.current_index]
        return None

    @property
    def _background_ocr_active(self) -> bool:
        return bool(self._active_ocr_job or self._ocr_jobs or self._ocr_queue_future)

    def _get_cached_base_image(self, path: Path) -> Image.Image | None:
        image = self.base_images.pop(path, None)
        if image is not None:
            self.base_images[path] = image
        return image

    def _cache_base_image(self, path: Path, image: Image.Image) -> None:
        self.base_images.pop(path, None)
        self.base_images[path] = image
        while len(self.base_images) > self.MAX_CACHED_PAGES:
            oldest_path = next(iter(self.base_images))
            self.base_images.pop(oldest_path, None)
            logger.info("Evicted PDF preview cache: path=%s", oldest_path)

    def _load_base_image(self, path: Path) -> Image.Image:
        image = self._get_cached_base_image(path)
        if image is None:
            image = self.pdf.render_first_page(path)
            self._cache_base_image(path, image)
        return image

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "添加 PDF", "", "PDF 文件 (*.pdf)")
        logger.info("Add files selected: count=%s", len(paths))
        self._append_paths([Path(path) for path in paths])

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择包含 PDF 的文件夹")
        if folder:
            logger.info("Add folder selected: %s", folder)
            paths = discover_pdfs(Path(folder))
            logger.info("PDF discovery complete: folder=%s count=%s", folder, len(paths))
            if not paths:
                QMessageBox.information(self, "未找到 PDF", "所选文件夹及其子文件夹中没有真实的 PDF 文件。")
                return
            self._append_paths(paths)

    def _append_paths(self, paths: list[Path]) -> None:
        known = {document.path.resolve() for document in self.documents}
        added = 0
        for path in paths:
            if not is_pdf_file(path):
                logger.warning("Ignored non-file PDF path: %s", path)
                continue
            resolved = path.resolve()
            if resolved in known:
                continue
            self.documents.append(DrawingDocument(resolved))
            known.add(resolved)
            added += 1
        logger.info("Documents added=%s total=%s", added, len(self.documents))
        self._refresh_list()
        if added and self.current_index < 0:
            self.file_list.setCurrentRow(0)
        elif not added and paths:
            self.statusBar().showMessage("所选 PDF 已在列表中")

    def _refresh_list(self) -> None:
        selected = self.current_index
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for document in self.documents:
            status_text = document.status.value
            if document.status == DocumentStatus.OCR_RUNNING:
                status_text = f"识别中 {document.ocr_progress}/{len(FieldKind)}"
            item = QListWidgetItem(f"{status_text}  ·  {document.path.name}")
            if document.status == DocumentStatus.CONFIRMED:
                item.setForeground(QColor("#148356"))
            elif document.status == DocumentStatus.OCR_QUEUED:
                item.setForeground(QColor("#8a5a00"))
            elif document.status == DocumentStatus.OCR_RUNNING:
                item.setForeground(QColor("#1769aa"))
            elif document.status == DocumentStatus.ERROR:
                item.setForeground(QColor("#bd2c2c"))
            self.file_list.addItem(item)
        if 0 <= selected < self.file_list.count():
            self.file_list.setCurrentRow(selected)
        self.file_list.blockSignals(False)
        confirmed = sum(document.status in (DocumentStatus.CONFIRMED, DocumentStatus.RENAMED) for document in self.documents)
        self.progress_label.setText(f"已确认 {confirmed} / {len(self.documents)}") if self.documents else self.progress_label.setText("尚未添加 PDF")
        self._update_action_buttons()

    def select_document(self, row: int) -> None:
        if not 0 <= row < len(self.documents):
            return
        self._invalidate_preview_render()
        self.current_index = row
        document = self.documents[row]
        self._reset_ocr_progress()
        logger.info(
            "Opening PDF preview: index=%s path=%s auto_suggest=%s",
            row,
            document.path,
            self.auto_suggest_action.isChecked(),
        )
        try:
            base = self._load_base_image(document.path)
            logger.info("PDF preview ready: path=%s size=%sx%s", document.path, base.width, base.height)
            image = self.pdf.rotate(base, document.rotation)
            self.preview.set_image(
                image,
                document.boxes,
                preview_dpi=self.pdf.PREVIEW_DPI,
                max_detail_dpi=self.pdf.MAX_DETAIL_DPI,
            )
            self._load_fields(document)
            if document.path.is_file():
                self.statusBar().showMessage(str(document.path))
            else:
                self.ocr_status.setText("文件可能已被移动；缓存画面仍可查看，修正前请重新添加实际PDF")
                self.statusBar().showMessage(f"文件不存在或已移动：{document.path}")
            if (
                document.status == DocumentStatus.PENDING
                and self.auto_suggest_action.isChecked()
                and not self._busy
                and not self._background_ocr_active
            ):
                QTimer.singleShot(100, self.suggest_current)
        except Exception as exc:
            logger.exception("Unable to open PDF: %s", document.path)
            document.status = DocumentStatus.ERROR
            document.error = str(exc)
            QMessageBox.warning(self, "无法打开 PDF", str(exc))
            self._refresh_list()

    def _load_fields(self, document: DrawingDocument) -> None:
        self._loading_fields = True
        for kind in FieldKind:
            field = document.fields[kind]
            self.field_edits[kind].setText(field.text)
            self.field_confidence[kind].setText(self._confidence_text(field.confidence, field.manually_edited))
        self.filename_preview.setText(document.proposed_filename)
        if document.status == DocumentStatus.OCR_RUNNING:
            self.ocr_status.setText(f"正在后台识别 {document.ocr_progress}/{len(FieldKind)}")
        elif document.status == DocumentStatus.OCR_QUEUED:
            self.ocr_status.setText("已加入后台识别队列")
        else:
            self.ocr_status.setText(document.error or document.status.value)
        self._loading_fields = False
        self._update_recognize_all_button()
        self._update_action_buttons()

    @staticmethod
    def _confidence_text(confidence: float | None, manual: bool) -> str:
        if manual:
            return "已手工修改"
        return "未识别" if confidence is None else f"OCR {confidence:.0%}"

    def _reset_ocr_progress(self) -> None:
        if self._busy:
            return
        document = self.current_document
        self._worker_progress_total = 0
        self.ocr_progress.setRange(0, len(FieldKind))
        if document and document.status == DocumentStatus.OCR_RUNNING:
            self.ocr_progress.setValue(document.ocr_progress)
            self.ocr_progress.setFormat(f"后台识别 {document.ocr_progress}/{len(FieldKind)}")
        elif document and document.status == DocumentStatus.OCR_QUEUED:
            self.ocr_progress.setValue(0)
            self.ocr_progress.setFormat("等待后台识别 0/3")
        else:
            self.ocr_progress.setValue(0)
            self.ocr_progress.setFormat("等待识别 0/3")

    def _update_recognize_all_button(self) -> None:
        document = self.current_document
        recognition_available = not self._busy and not self._background_ocr_active
        for kind, button in self.recognize_buttons.items():
            has_box = bool(document and kind in document.boxes)
            button.setEnabled(has_box and recognition_available)
            if document is None:
                tooltip = "请先添加并选择 PDF"
            elif not has_box:
                tooltip = f"请先框选{kind.label}"
            elif not recognition_available:
                tooltip = "后台识别队列正在运行；可继续框选并使用蓝色主按钮提交下一份"
            else:
                tooltip = f"只重新识别{kind.label}，保留另外两个字段"
            button.setToolTip(tooltip)
        ready = bool(document and document.all_boxes_present and recognition_available)
        self.recognize_all_button.setEnabled(ready)
        if document is None:
            tooltip = "请先添加并选择 PDF"
        elif not document.all_boxes_present:
            missing = [kind.label for kind in FieldKind if kind not in document.boxes]
            tooltip = "请先框选：" + "、".join(missing)
        elif not recognition_available:
            tooltip = "后台识别队列正在运行；可继续框选并使用蓝色主按钮提交下一份"
        else:
            tooltip = "依次识别物料编码、名称和工序编号"
        self.recognize_all_button.setToolTip(tooltip)

    def _update_action_buttons(self) -> None:
        document = self.current_document
        has_document = document is not None
        is_renamed = bool(document and document.status == DocumentStatus.RENAMED)
        proposed_changed = bool(
            document
            and document.proposed_filename
            and document.proposed_filename != document.path.name
        )
        source_exists = bool(document and document.path.is_file())
        queued = bool(
            document
            and document.status in (DocumentStatus.OCR_QUEUED, DocumentStatus.OCR_RUNNING)
        )
        ready_to_confirm = bool(document and document.all_fields_filled)
        ready_to_queue = bool(document and document.all_boxes_present and not ready_to_confirm)
        if queued:
            self.confirm_button.setText("正在后台识别")
        elif ready_to_queue:
            self.confirm_button.setText("识别并下一份")
        else:
            self.confirm_button.setText("确认并下一份")
        self.confirm_button.setEnabled(
            has_document
            and not is_renamed
            and not queued
            and not self._busy
            and (ready_to_confirm or ready_to_queue)
        )
        self.rename_single_button.setEnabled(
            has_document and proposed_changed and source_exists and not self._busy
        )
        self.execute_button.setEnabled(
            bool(self.documents) and not self._busy and not self._background_ocr_active
        )
        if not has_document:
            self.rename_single_button.setToolTip("请先选择一个PDF")
        elif not source_exists:
            self.rename_single_button.setToolTip("文件已被移动；请重新添加移动后的PDF")
        elif not proposed_changed:
            self.rename_single_button.setToolTip("重新框选、识别或修改字段后，新文件名发生变化才可使用")
        else:
            self.rename_single_button.setToolTip("仅重新命名当前选中的PDF，不影响其他文件")

    def _activate_kind(self, kind: FieldKind | None) -> None:
        self.preview.set_active_kind(kind)
        if kind is None:
            self.instruction.setText(
                "手动框选：①点击下方彩色标签　②在图纸文字上按住鼠标左键拖拽　"
                "③松开完成；框可拖动，右下角可缩放；按 Esc 退出框选后可拖动图纸"
            )
            self.statusBar().showMessage("浏览模式：按住鼠标左键可拖动图纸；点击彩色标签可继续框选")
            return
        color_label = {
            FieldKind.MATERIAL: "蓝色",
            FieldKind.NAME: "绿色",
            FieldKind.PROCESS: "橙色",
        }[kind]
        self.instruction.setText(
            f"正在添加【{kind.label}】{color_label}框：在对应文字上按住左键拖拽；按 Esc 退出框选并拖动图纸"
        )
        self.statusBar().showMessage(f"框选模式：{kind.label}（{color_label}框）；Esc 返回浏览模式")

    def _auto_suggest_toggled(self, enabled: bool) -> None:
        self.auto_suggest_action.setText(f"自动建议框：{'开' if enabled else '关'}")
        logger.info("Automatic company-anchor suggestions toggled: enabled=%s", enabled)
        document = self.current_document
        if (
            enabled
            and document
            and document.status == DocumentStatus.PENDING
            and not self._busy
            and not self._background_ocr_active
        ):
            QTimer.singleShot(100, self.suggest_current)

    def show_log_dialog(self) -> None:
        if self.diagnostics is None:
            QMessageBox.information(self, "问题反馈日志", "当前运行环境未启用日志。")
            return
        logger.info("Opening feedback log dialog")
        LogDialog(self.diagnostics, self).exec()

    def show_history_dialog(self) -> None:
        logger.info("Opening selection history dialog: root=%s", self.history.root)
        HistoryDialog(self.history, self).exec()

    def _save_history_snapshot(self, document: DrawingDocument, event_type: str) -> None:
        payload = self.history.create_payload(document, event_type)
        proposed_filename = document.proposed_filename
        screenshot = self.grab().toImage().copy()
        future = self.history_executor.submit(self.history.save, payload, screenshot)
        self._history_futures.add(future)

        def finished(completed: Future[Any]) -> None:
            try:
                entry = completed.result()
                logger.info(
                    "Selection history saved: event=%s proposed=%s screenshot=%s",
                    event_type,
                    proposed_filename,
                    entry.screenshot_path,
                )
            except Exception:
                logger.exception("Unable to save selection history")
            finally:
                self._history_futures.discard(completed)

        future.add_done_callback(finished)

    def show_usage_help(self) -> None:
        QMessageBox.information(
            self,
            "手动添加高亮框",
            "操作步骤：\n\n"
            "1. 在左侧选择要处理的PDF。\n"
            "2. 点击下方彩色标签：\n"
            "   • 蓝色：物料编码\n"
            "   • 绿色：名称\n"
            "   • 橙色：工序编号\n"
            "3. 鼠标移到PDF图纸，在对应文字外围按住左键并拖拽。\n"
            "4. 松开鼠标后生成带标签的高亮框。\n"
            "5. 完成一个框后会自动进入下一个框选标签。\n"
            "6. 随时按 Esc 退出框选模式，之后可按住鼠标左键拖动图纸。\n"
            "7. 拖动框体可以移动；拖动右下角可以调整大小。\n"
            "8. 如需重画，再次点击彩色标签并重新拖拽；也可选中框后按 Delete。\n"
            "9. 可点击每个字段下方的“仅识别此框”单独识别；三个框完成后也可点击“一键识别三个框”。\n"
            "10. 三个框完成但尚未识别时，蓝色主按钮会显示“识别并下一份”；点击后任务进入后台队列，界面立即打开下一份PDF。\n"
            "11. 左侧显示“等待识别、识别中、待确认”；后台完成后必须返回该文件人工核对，再点击“确认并下一份”。\n"
            "12. OCR失败时，可以直接在下方输入框手工填写。确认时会保存现场画面和框选数据，可从左上角“历史记录”查看。\n"
            "13. 批量重命名后请先检查新文件名再移动PDF。发现错误时，选中文件、重新框选和识别，"
            "然后点击“重命名选中文件”。\n\n"
            "快捷键：1=物料编码，2=名称，3=工序编号，Esc=退出框选。",
        )

    def _select_kind_button(self, kind: FieldKind) -> None:
        button = self.kind_buttons[kind]
        button.setChecked(True)
        self._activate_kind(kind)

    def exit_box_selection(self) -> None:
        """Leave drawing mode without deleting existing boxes."""

        self.kind_group.setExclusive(False)
        for button in self.kind_buttons.values():
            button.setChecked(False)
        self.kind_group.setExclusive(True)
        self._activate_kind(None)
        logger.info("Box selection mode exited with Escape")

    def _save_current_boxes(self) -> None:
        document = self.current_document
        if not document:
            return
        boxes = self.preview.normalized_boxes()
        changed = boxes != document.boxes
        if changed:
            document.ocr_revision += 1
        document.boxes = boxes
        if document.status not in (DocumentStatus.CONFIRMED, DocumentStatus.RENAMED):
            if changed or document.status not in (DocumentStatus.OCR_QUEUED, DocumentStatus.OCR_RUNNING):
                document.status = (
                    DocumentStatus.NEEDS_CONFIRMATION if document.boxes else DocumentStatus.NEEDS_BOXES
                )
        self._update_recognize_all_button()
        self._update_action_buttons()

    def _box_finished(self, kind: FieldKind) -> None:
        self._save_current_boxes()
        kinds = list(FieldKind)
        next_index = kinds.index(kind) + 1
        if next_index < len(kinds):
            self._select_kind_button(kinds[next_index])
            self.statusBar().showMessage(
                f"{kind.label}框已添加，已自动进入{kinds[next_index].label}框选；按 Esc 可退出并拖动图纸"
            )
        else:
            self.exit_box_selection()
        if self.current_document and self.current_document.all_boxes_present:
            self.ocr_status.setText("三个框已添加，可点击蓝色“识别并下一份”提交后台任务")
        else:
            self.ocr_status.setText(f"{kind.label}框已添加，请继续完成其余识别框")

    def _field_edited(self, kind: FieldKind, text: str) -> None:
        if self._loading_fields:
            return
        document = self.current_document
        if not document:
            return
        field = document.fields[kind]
        field.text = text
        field.manually_edited = True
        field.message = "手工输入"
        self.field_confidence[kind].setText("已手工修改")
        if document.status not in (
            DocumentStatus.RENAMED,
            DocumentStatus.OCR_QUEUED,
            DocumentStatus.OCR_RUNNING,
        ):
            document.status = DocumentStatus.NEEDS_CONFIRMATION
        self._update_filename(document)

    def _update_filename(self, document: DrawingDocument) -> None:
        try:
            document.proposed_filename = build_filename_from_fields(document.fields)
        except ValueError:
            document.proposed_filename = ""
        if document is self.current_document:
            self.filename_preview.setText(document.proposed_filename)
            self._update_action_buttons()

    @staticmethod
    def _mark_document_for_review(
        document: DrawingDocument,
        status: DocumentStatus = DocumentStatus.NEEDS_CONFIRMATION,
    ) -> None:
        # A physically renamed file must retain this identity while it is being
        # re-framed/re-recognized, otherwise the single-file correction action
        # can no longer distinguish it from an unprocessed document.
        if document.status != DocumentStatus.RENAMED:
            document.status = status

    def clear_field(self, kind: FieldKind) -> None:
        document = self.current_document
        if not document:
            return
        document.fields[kind].text = ""
        document.fields[kind].confidence = None
        document.fields[kind].manually_edited = False
        self.field_edits[kind].clear()
        self.field_confidence[kind].setText("未识别")
        self.preview.remove_box(kind)
        self._update_filename(document)

    def _delete_selected_box(self) -> None:
        for kind, item in list(self.preview.roi_items.items()):
            if item.isSelected():
                self.preview.remove_box(kind)
                return

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.busy_bar.setVisible(busy)
        self.suggest_action.setEnabled(not busy and not self._background_ocr_active)
        self.cancel_ocr_action.setEnabled(busy or self._background_ocr_active)
        self._update_recognize_all_button()
        self._update_action_buttons()
        if message:
            self.ocr_status.setText(message)
            self.statusBar().showMessage(message)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor) if busy else QApplication.restoreOverrideCursor()

    def _run_worker(
        self,
        function: Callable[[], Any],
        finished: Callable[[Any], None],
        message: str,
        progress_total: int = 0,
    ) -> None:
        if self._busy or self._background_ocr_active:
            self.statusBar().showMessage("OCR正在处理中，请稍候")
            return
        self._worker_progress_queue = SimpleQueue()
        self._worker_progress_total = progress_total
        if progress_total:
            self.ocr_progress.setRange(0, progress_total)
            self.ocr_progress.setValue(0)
            self.ocr_progress.setFormat(f"准备识别 0/{progress_total}")
        self._set_busy(True, message)
        logger.info("Background OCR task starting: %s", message)
        self._worker_callback = finished
        self._worker_future = self.executor.submit(function)
        self._worker_timer.start()

    def _report_worker_progress(self, value: int, total: int, message: str) -> None:
        self._worker_progress_queue.put((value, total, message))

    def _drain_worker_progress(self) -> None:
        while True:
            try:
                value, total, message = self._worker_progress_queue.get_nowait()
            except Empty:
                return
            self.ocr_progress.setRange(0, total)
            self.ocr_progress.setValue(value)
            self.ocr_progress.setFormat(f"{message}  {value}/{total}")
            self.ocr_status.setText(message)
            self.statusBar().showMessage(f"{message}（{value}/{total}）")

    def _poll_worker(self) -> None:
        self._drain_worker_progress()
        future = self._worker_future
        if future is None or not future.done():
            return
        self._worker_timer.stop()
        callback = self._worker_callback
        self._worker_future = None
        self._worker_callback = None
        try:
            result = future.result()
        except Exception as exc:  # pragma: no cover - native/optional runtime boundary
            logger.exception("Background OCR controller failed")
            self._worker_failed(str(exc))
            return
        if callback is not None:
            self._worker_done(result, callback)

    def _worker_done(self, result: Any, callback: Callable[[Any], None]) -> None:
        self._set_busy(False)
        if self._worker_progress_total:
            total = self._worker_progress_total
            self.ocr_progress.setValue(total)
            self.ocr_progress.setFormat(f"识别完成 {total}/{total}")
            self._worker_progress_total = 0
        logger.info("Background OCR task completed")
        callback(result)
        self._close_after_cancel_if_needed()

    def _worker_failed(self, message: str) -> None:
        self._set_busy(False)
        if self._worker_progress_total:
            total = self._worker_progress_total
            self.ocr_progress.setFormat(f"识别中止 {self.ocr_progress.value()}/{total}")
            self._worker_progress_total = 0
        logger.error("Background OCR task reported failure: %s", message)
        if message == "OCR任务已取消":
            self.ocr_status.setText("识别任务已取消，可继续框选或手工输入")
            self.statusBar().showMessage("OCR识别已取消")
            self._close_after_cancel_if_needed()
            return
        document = self.current_document
        if document:
            document.error = message
            if document.status == DocumentStatus.PENDING:
                document.status = DocumentStatus.NEEDS_BOXES
        self.ocr_status.setText("无法识别，请手工框选或输入")
        QMessageBox.warning(self, "OCR提示", f"{message}\n\n仍可手工框选并直接输入三个字段。")
        self._refresh_list()
        self._close_after_cancel_if_needed()

    def _request_high_resolution(self, rect: NormalizedRect, dpi: int) -> None:
        document = self.current_document
        if document is None or not document.path.is_file():
            return
        self._preview_request_serial += 1
        request = PreviewRenderRequest(
            serial=self._preview_request_serial,
            path=document.path,
            rotation=document.rotation,
            rect=rect,
            dpi=dpi,
        )
        self._pending_preview_request = request
        logger.debug(
            "High-resolution preview requested: path=%s rotation=%s dpi=%s rect=(%.4f,%.4f,%.4f,%.4f)",
            request.path,
            request.rotation,
            request.dpi,
            request.rect.x,
            request.rect.y,
            request.rect.width,
            request.rect.height,
        )
        if self._preview_future is None:
            self._start_pending_preview_render()

    def _cancel_high_resolution(self) -> None:
        self._preview_request_serial += 1
        self._pending_preview_request = None
        self.preview.clear_high_resolution(notify=False)

    def _invalidate_preview_render(self) -> None:
        self._preview_request_serial += 1
        self._pending_preview_request = None
        if hasattr(self, "preview"):
            self.preview.clear_high_resolution(notify=False)

    def _start_pending_preview_render(self) -> None:
        request = self._pending_preview_request
        if request is None or self._preview_future is not None:
            return
        self._pending_preview_request = None

        def render() -> tuple[PreviewRenderRequest, Image.Image]:
            image = self.pdf.render_region(
                request.path,
                request.rect,
                request.rotation,
                request.dpi,
            )
            return request, image

        self._preview_future = self.preview_executor.submit(render)
        self._preview_timer.start()

    def _poll_preview_render(self) -> None:
        future = self._preview_future
        if future is None:
            self._preview_timer.stop()
            self._start_pending_preview_render()
            return
        if not future.done():
            return
        self._preview_future = None
        try:
            request, image = future.result()
        except Exception:  # pragma: no cover - optional native renderer boundary
            logger.exception("High-resolution preview render failed; keeping low-resolution preview")
        else:
            current = self.current_document
            if (
                request.serial == self._preview_request_serial
                and current is not None
                and current.path == request.path
                and current.rotation == request.rotation
            ):
                self.preview.set_high_resolution_region(image, request.rect)
                logger.info(
                    "High-resolution preview ready: path=%s dpi=%s size=%sx%s",
                    request.path,
                    request.dpi,
                    image.width,
                    image.height,
                )
            else:
                logger.debug("Discarded stale high-resolution preview: path=%s", request.path)
        if self._pending_preview_request is not None:
            self._start_pending_preview_render()
        else:
            self._preview_timer.stop()

    def cancel_current_ocr(self) -> None:
        if not self._busy and not self._background_ocr_active:
            return
        logger.info("User requested OCR cancellation")
        self.ocr.cancel_current()
        if self._background_ocr_active:
            self.ocr_status.setText("正在取消当前后台识别任务…")
        else:
            self.ocr_status.setText("正在取消识别任务…")
        self.statusBar().showMessage("正在终止OCR独立进程…")

    def _close_after_cancel_if_needed(self) -> None:
        if not self._pending_close:
            return
        self._pending_close = False
        QTimer.singleShot(0, self.close)

    def suggest_current(self) -> None:
        if self._background_ocr_active:
            self.statusBar().showMessage("后台识别队列运行中，请先继续框选；建议框功能稍后可用")
            return
        document = self.current_document
        if not document:
            return
        base = self._load_base_image(document.path)
        path = document.path
        logger.info("Company-anchor suggestion requested for current PDF: %s", path)
        self._run_worker(
            lambda: (path, self.ocr.suggest(base.copy())),
            self._suggestion_ready,
            "正在查找公司名称并生成建议框…最多等待120秒，可点击“取消识别”",
        )

    def _suggestion_ready(self, payload: tuple[Path, SuggestionResult]) -> None:
        path, result = payload
        document = next((item for item in self.documents if item.path == path), None)
        if document is None:
            return
        document.rotation = result.rotation
        logger.info(
            "Suggestion ready: path=%s rotation=%s anchor_found=%s recognized=%s",
            path,
            result.rotation,
            result.anchor_found,
            [kind.value for kind in result.recognized],
        )
        document.boxes = result.boxes
        for kind, (text, confidence) in result.recognized.items():
            if not document.fields[kind].manually_edited:
                document.fields[kind].text = text
                document.fields[kind].confidence = confidence
        self._mark_document_for_review(
            document,
            DocumentStatus.NEEDS_CONFIRMATION if result.boxes else DocumentStatus.NEEDS_BOXES,
        )
        document.error = "" if result.anchor_found else result.message
        self._update_filename(document)
        if document is self.current_document:
            self._invalidate_preview_render()
            image = self.pdf.rotate(self._load_base_image(path), document.rotation)
            self.preview.set_image(
                image,
                document.boxes,
                preview_dpi=self.pdf.PREVIEW_DPI,
                max_detail_dpi=self.pdf.MAX_DETAIL_DPI,
            )
            self._load_fields(document)
            self.ocr_status.setText(result.message)
        self._refresh_list()

    def recognize_box(self, kind: FieldKind) -> None:
        if self._background_ocr_active:
            self.statusBar().showMessage("后台识别队列运行中，可使用蓝色主按钮提交下一份")
            return
        document = self.current_document
        if not document:
            return
        self._save_current_boxes()
        rect = document.boxes.get(kind)
        if rect is None:
            self.ocr_status.setText(f"请先框选{kind.label}")
            return
        path = document.path
        logger.info("Single-box OCR requested: path=%s field=%s", path, kind.value)
        image = self.pdf.rotate(self._load_base_image(path), document.rotation)
        crop = self.pdf.crop(image, rect)
        self._run_worker(
            lambda: (path, kind, *self.ocr.recognize_text(crop)),
            self._recognition_ready,
            f"正在识别{kind.label}…最多等待20秒，可点击“取消识别”",
        )

    def _recognition_ready(self, payload: tuple[Path, FieldKind, str, float | None]) -> None:
        path, kind, text, confidence = payload
        document = next((item for item in self.documents if item.path == path), None)
        if document is None:
            return
        field = document.fields[kind]
        logger.info(
            "Single-box OCR ready: path=%s field=%s has_text=%s confidence=%s",
            path,
            kind.value,
            bool(text),
            confidence,
        )
        if text:
            field.text = text
            field.confidence = confidence
            field.manually_edited = False
            field.message = ""
            message = f"{kind.label}识别完成，请人工核对"
        else:
            field.message = "无法识别，请手工输入"
            message = f"未识别出{kind.label}，请手工输入"
        self._mark_document_for_review(document)
        self._update_filename(document)
        if document is self.current_document:
            self._load_fields(document)
            self.ocr_status.setText(message)
        self._refresh_list()

    def recognize_all_boxes(self) -> None:
        if self._background_ocr_active:
            self.statusBar().showMessage("后台识别队列运行中，可使用蓝色主按钮提交下一份")
            return
        document = self.current_document
        if not document:
            return
        self._save_current_boxes()
        missing = [kind.label for kind in FieldKind if kind not in document.boxes]
        if missing:
            QMessageBox.information(self, "缺少识别框", "请先框选：" + "、".join(missing))
            return
        path = document.path
        logger.info("Current-page three-box OCR requested: path=%s", path)
        crops = self._capture_ocr_crops(document)

        def work() -> tuple[Path, dict[FieldKind, tuple[str, float | None]]]:
            def progress(value: int, total: int, field: str) -> None:
                kind = FieldKind(field) if field else None
                if value == 0 and kind is not None:
                    message = f"正在识别{kind.label}"
                elif kind is not None:
                    message = f"{kind.label}识别完成"
                else:
                    message = "正在识别"
                self._report_worker_progress(value, total, message)

            return path, self.ocr.recognize_batch(crops, progress)

        self._run_worker(
            work,
            self._all_recognition_ready,
            "正在一键识别三个框…",
            progress_total=len(FieldKind),
        )

    def _all_recognition_ready(self, payload: tuple[Path, dict[FieldKind, tuple[str, float | None]]]) -> None:
        path, values = payload
        document = next((item for item in self.documents if item.path == path), None)
        if document is None:
            return
        failed = self._apply_recognition_values(document, values)
        if document is self.current_document:
            self._load_fields(document)
            self.ocr_status.setText("请手工输入：" + "、".join(failed) if failed else "三个字段识别完成，请人工核对")
        self._refresh_list()

    def _capture_ocr_crops(self, document: DrawingDocument) -> dict[FieldKind, Image.Image]:
        base = self._load_base_image(document.path)
        image = self.pdf.rotate(base, document.rotation)
        boxes = dict(document.boxes)
        return {kind: self.pdf.crop(image, boxes[kind]) for kind in FieldKind}

    def _apply_recognition_values(
        self,
        document: DrawingDocument,
        values: dict[FieldKind, tuple[str, float | None]],
        *,
        preserve_manual: bool = False,
    ) -> list[str]:
        failed: list[str] = []
        for kind in FieldKind:
            text, confidence = values.get(kind, ("", None))
            if preserve_manual and document.fields[kind].manually_edited:
                continue
            if text:
                document.fields[kind].text = text
                document.fields[kind].confidence = confidence
                document.fields[kind].manually_edited = False
                document.fields[kind].message = ""
            else:
                failed.append(kind.label)
                document.fields[kind].message = "无法识别，请手工输入"
        self._mark_document_for_review(document)
        document.error = ""
        self._update_filename(document)
        return failed

    def _enqueue_background_ocr(self, document: DrawingDocument) -> bool:
        if document.status in (DocumentStatus.OCR_QUEUED, DocumentStatus.OCR_RUNNING):
            return False
        try:
            crops = self._capture_ocr_crops(document)
        except Exception as exc:
            logger.exception("Unable to prepare background OCR crops: path=%s", document.path)
            QMessageBox.warning(self, "无法开始识别", f"无法读取框选区域：{exc}")
            return False
        job = OcrBatchJob(document=document, revision=document.ocr_revision, crops=crops)
        document.status = DocumentStatus.OCR_QUEUED
        document.ocr_progress = 0
        document.error = ""
        self._ocr_jobs.append(job)
        logger.info(
            "Background OCR queued: path=%s revision=%s waiting=%s",
            document.path,
            job.revision,
            len(self._ocr_jobs),
        )
        self._refresh_list()
        self._start_next_ocr_job()
        return True

    def _start_next_ocr_job(self) -> None:
        if self._ocr_queue_future is not None:
            return
        while self._ocr_jobs:
            job = self._ocr_jobs.popleft()
            document = job.document
            if job.revision != document.ocr_revision or document.status != DocumentStatus.OCR_QUEUED:
                logger.info("Discarded stale queued OCR job: path=%s", document.path)
                continue
            self._active_ocr_job = job
            document.status = DocumentStatus.OCR_RUNNING
            document.ocr_progress = 0

            def progress(value: int, total: int, field: str, active_job: OcrBatchJob = job) -> None:
                self._ocr_queue_progress.put((active_job, value, total, field))

            self._ocr_queue_future = self.ocr_queue_executor.submit(
                self.ocr.recognize_batch,
                job.crops,
                progress,
            )
            self._ocr_queue_timer.start()
            logger.info("Background OCR started: path=%s", document.path)
            self._sync_ocr_controls()
            self._refresh_list()
            return
        self._ocr_queue_timer.stop()
        self._sync_ocr_controls()
        self._close_after_cancel_if_needed()

    def _sync_ocr_controls(self) -> None:
        self.suggest_action.setEnabled(not self._busy and not self._background_ocr_active)
        self.cancel_ocr_action.setEnabled(self._busy or self._background_ocr_active)
        self._update_recognize_all_button()
        self._update_action_buttons()

    def _drain_ocr_queue_progress(self) -> None:
        changed = False
        while True:
            try:
                job, value, total, field = self._ocr_queue_progress.get_nowait()
            except Empty:
                break
            if job is not self._active_ocr_job:
                continue
            document = job.document
            document.ocr_progress = value
            changed = True
            try:
                field_label = FieldKind(field).label if field else "字段"
            except ValueError:
                field_label = "字段"
            if document is self.current_document:
                self.ocr_progress.setRange(0, total)
                self.ocr_progress.setValue(value)
                self.ocr_progress.setFormat(f"后台识别 {value}/{total}")
                self.ocr_status.setText(f"正在后台识别{field_label} {value}/{total}")
            self.statusBar().showMessage(
                f"后台识别：{document.path.name} {value}/{total}；等待 {len(self._ocr_jobs)} 份"
            )
        if changed:
            self._refresh_list()

    def _poll_ocr_queue(self) -> None:
        self._drain_ocr_queue_progress()
        future = self._ocr_queue_future
        job = self._active_ocr_job
        if future is None or job is None or not future.done():
            return
        self._ocr_queue_future = None
        self._active_ocr_job = None
        document = job.document
        try:
            values = future.result()
        except OcrCancelledError:
            logger.info("Background OCR cancelled: path=%s", document.path)
            if job.revision == document.ocr_revision:
                document.status = (
                    DocumentStatus.NEEDS_CONFIRMATION if document.boxes else DocumentStatus.NEEDS_BOXES
                )
                document.error = "识别任务已取消，可重新提交或手工输入"
                document.ocr_progress = 0
        except Exception as exc:  # pragma: no cover - native/optional runtime boundary
            logger.exception("Background OCR failed: path=%s", document.path)
            if job.revision == document.ocr_revision:
                document.status = DocumentStatus.ERROR
                document.error = f"后台识别失败：{exc}"
                document.ocr_progress = 0
        else:
            if job.revision != document.ocr_revision:
                logger.info("Discarded stale background OCR result: path=%s", document.path)
                newer_job_waiting = any(
                    queued.document is document and queued.revision == document.ocr_revision
                    for queued in self._ocr_jobs
                )
                if not newer_job_waiting:
                    document.error = "框选已发生变化，旧的识别结果已丢弃，请重新识别"
            else:
                failed = self._apply_recognition_values(document, values, preserve_manual=True)
                document.ocr_progress = len(FieldKind)
                document.error = "请手工输入：" + "、".join(failed) if failed else ""
                logger.info(
                    "Background OCR completed: path=%s failed=%s",
                    document.path,
                    failed,
                )
        if document is self.current_document:
            self._load_fields(document)
        self._refresh_list()
        self._sync_ocr_controls()
        QTimer.singleShot(0, self._start_next_ocr_job)

    def rotate_current(self, degrees_ccw: int) -> None:
        document = self.current_document
        if not document:
            return
        self._save_current_boxes()
        transformed: dict[FieldKind, NormalizedRect] = {}
        for kind, rect in document.boxes.items():
            if degrees_ccw % 360 == 90:
                transformed[kind] = NormalizedRect(rect.y, 1 - rect.x - rect.width, rect.height, rect.width).clamped()
            else:
                transformed[kind] = NormalizedRect(1 - rect.y - rect.height, rect.x, rect.height, rect.width).clamped()
        document.boxes = transformed
        document.rotation = (document.rotation + degrees_ccw) % 360
        self._invalidate_preview_render()
        document.ocr_revision += 1
        if document.status not in (DocumentStatus.CONFIRMED, DocumentStatus.RENAMED):
            document.status = DocumentStatus.NEEDS_CONFIRMATION
        image = self.pdf.rotate(self._load_base_image(document.path), document.rotation)
        self.preview.set_image(
            image,
            document.boxes,
            preview_dpi=self.pdf.PREVIEW_DPI,
            max_detail_dpi=self.pdf.MAX_DETAIL_DPI,
        )

    def confirm_and_next(self) -> None:
        document = self.current_document
        if not document:
            return
        if document.status == DocumentStatus.RENAMED:
            QMessageBox.information(
                self,
                "单文件修正",
                "该文件已经重命名。重新框选或修改字段后，请使用“重命名选中文件”。",
            )
            return
        self._save_current_boxes()
        for kind in FieldKind:
            document.fields[kind].text = self.field_edits[kind].text().strip()
        self._update_filename(document)
        if not document.proposed_filename:
            if document.all_boxes_present and self._enqueue_background_ocr(document):
                self._advance_to_next_work_item()
                return
            QMessageBox.warning(
                self,
                "信息不完整",
                "请先完成三个识别框，或手工填写物料编码、名称和工序编号。",
            )
            return
        reserved = {
            item.path.with_name(item.confirmed_filename)
            for item in self.documents
            if item is not document and item.confirmed_filename
        }
        problem = validate_destination(document.path, document.proposed_filename, reserved)
        if problem:
            QMessageBox.warning(self, "文件名冲突", problem)
            return
        document.confirmed_filename = document.proposed_filename
        document.status = DocumentStatus.CONFIRMED
        document.error = ""
        logger.info(
            "Document confirmed: source=%s proposed=%s",
            document.path,
            document.confirmed_filename,
        )
        self._save_history_snapshot(document, "确认并等待批量重命名")
        self._refresh_list()
        if not self._advance_to_next_work_item():
            self.ocr_status.setText("全部文件已确认，可以执行批量重命名")
            QMessageBox.information(self, "确认完成", "全部文件已人工确认。请检查列表后执行批量重命名。")

    def _advance_to_next_work_item(self) -> bool:
        if not self.documents:
            return False
        current = self.current_index
        order = list(range(current + 1, len(self.documents))) + list(range(0, current))
        preferred_statuses = (DocumentStatus.PENDING, DocumentStatus.NEEDS_BOXES)
        review_statuses = (DocumentStatus.NEEDS_CONFIRMATION, DocumentStatus.ERROR)
        for statuses in (preferred_statuses, review_statuses):
            next_row = next((index for index in order if self.documents[index].status in statuses), -1)
            if next_row >= 0:
                self.file_list.setCurrentRow(next_row)
                return True
        if self._background_ocr_active:
            self.ocr_status.setText("已提交后台识别；可在左侧查看进度，完成后请逐份人工确认")
            self.statusBar().showMessage("所有已框选文件均已提交后台识别，请等待完成后人工确认")
        return False

    def rename_selected_file(self) -> None:
        if self._background_ocr_active:
            QMessageBox.information(self, "后台识别进行中", "请等待识别队列完成后再重命名文件。")
            return
        document = self.current_document
        if document is None:
            QMessageBox.information(self, "单文件重命名", "请先在左侧选择一个PDF。")
            return
        if not document.path.is_file():
            QMessageBox.warning(
                self,
                "文件已移动",
                f"找不到当前文件：\n{document.path}\n\n"
                "请使用“添加PDF”重新导入移动后的文件，再进行框选和重命名。",
            )
            return
        self._save_current_boxes()
        for kind in FieldKind:
            document.fields[kind].text = self.field_edits[kind].text().strip()
        self._update_filename(document)
        if not document.proposed_filename:
            QMessageBox.warning(self, "信息不完整", "物料编码、名称和工序编号均不能为空。")
            return
        if document.proposed_filename == document.path.name:
            QMessageBox.information(self, "无需重命名", "新文件名与当前文件名相同。")
            return
        problem = validate_destination(document.path, document.proposed_filename)
        if problem:
            QMessageBox.warning(self, "文件名冲突", problem)
            return
        answer = QMessageBox.question(
            self,
            "重命名选中文件",
            f"仅修改当前文件：\n\n{document.path.name}\n↓\n{document.proposed_filename}\n\n"
            "确认重新命名吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        old_path = document.path
        log_directory = old_path.parent / "重命名日志"
        self._save_history_snapshot(document, "单文件重命名确认")
        self._invalidate_preview_render()
        try:
            result = self.renamer.execute_one(
                document,
                document.proposed_filename,
                log_directory,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "单文件重命名失败", str(exc))
            return
        if not result.success or result.destination is None:
            QMessageBox.warning(self, "单文件重命名失败", result.message)
            self._refresh_list()
            return
        cached = self.base_images.pop(old_path, None)
        if cached is not None:
            self._cache_base_image(document.path, cached)
        document.proposed_filename = document.path.name
        logger.info("Single-file correction completed: source=%s destination=%s", old_path, document.path)
        self._refresh_list()
        self._load_fields(document)
        self.statusBar().showMessage(f"单文件修正完成：{document.path}")
        QMessageBox.information(
            self,
            "单文件修正完成",
            f"文件已重命名为：\n{document.path.name}\n\n请再次检查左侧名称和历史记录。",
        )

    def execute_rename(self) -> None:
        if not self.documents:
            return
        if self._background_ocr_active:
            QMessageBox.information(self, "后台识别进行中", "请等待识别队列完成并逐份人工确认后再批量重命名。")
            return
        errors = self.renamer.validate_batch(self.documents)
        if errors:
            QMessageBox.warning(self, "暂不能重命名", "\n".join(errors[:12]))
            return
        answer = QMessageBox.question(
            self,
            "执行批量重命名",
            f"即将重命名 {len(self.documents)} 个原始 PDF。\n不会覆盖同名文件，操作记录将保存在首个PDF目录的“重命名日志”文件夹。\n\n确认继续吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        log_directory = self.documents[0].path.parent / "重命名日志"
        logger.info("Batch rename confirmed by user: count=%s log_directory=%s", len(self.documents), log_directory)
        old_paths = {id(document): document.path for document in self.documents}
        self._invalidate_preview_render()
        try:
            results = self.renamer.execute(self.documents, log_directory)
        except ValueError as exc:
            logger.exception("Batch rename validation/execution failed")
            QMessageBox.warning(self, "重命名失败", str(exc))
            return
        for document in self.documents:
            old_path = old_paths[id(document)]
            if document.path != old_path:
                cached = self.base_images.pop(old_path, None)
                if cached is not None:
                    self._cache_base_image(document.path, cached)
        success = sum(result.success for result in results)
        logger.info("Batch rename finished: success=%s total=%s", success, len(results))
        self._refresh_list()
        QMessageBox.warning(
            self,
            "重命名完成，请先检查",
            f"成功重命名 {success}/{len(results)} 个文件。\n\n"
            "请先检查左侧显示的新文件名，并点击“历史记录”核对框选画面；"
            "确认无误后再移动这些PDF。\n\n"
            "如果发现单个错误：选中文件 → 重新框选 → 一键识别 → 点击“重命名选中文件”。\n\n"
            f"操作日志：{log_directory}",
        )

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        logger.info(
            "Window close requested; busy=%s background_ocr=%s documents=%s",
            self._busy,
            self._background_ocr_active,
            len(self.documents),
        )
        if self._busy or self._background_ocr_active:
            answer = QMessageBox.question(
                self,
                "识别任务正在运行",
                "当前OCR任务或等待队列尚未结束。是否取消全部识别任务并关闭软件？",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._pending_close = True
                self._ocr_jobs.clear()
                self.cancel_current_ocr()
            event.ignore()
            return
        self._worker_timer.stop()
        self._ocr_queue_timer.stop()
        self._preview_timer.stop()
        self._pending_preview_request = None
        self._preview_request_serial += 1
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.ocr_queue_executor.shutdown(wait=False, cancel_futures=True)
        self.preview_executor.shutdown(wait=False, cancel_futures=True)
        self.history_executor.shutdown(wait=True, cancel_futures=False)
        event.accept()
