from __future__ import annotations

import re

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..history_service import HistoryEntry, HistoryService


class HistoryDialog(QDialog):
    def __init__(self, service: HistoryService, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.service = service
        self.all_entries: list[HistoryEntry] = []
        self.entries: list[HistoryEntry] = []
        self.current_pixmap: QPixmap | None = None
        self.setWindowTitle("框选历史记录")
        self.resize(1180, 760)

        root = QVBoxLayout(self)
        path_label = QLabel(f"保存目录：{service.root}")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(path_label)

        search_row = QHBoxLayout()
        search_label = QLabel("搜索：")
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("输入物料编码、名称或工序/工艺编码（支持正则表达式）")
        self.search_edit.setToolTip(
            "实时搜索三个字段，任意一个字段匹配即显示；例如：B\\.0044、泵体|端盖、CP41\\.10[12]"
        )
        self.search_edit.textChanged.connect(self._apply_filter)
        self.search_status = QLabel("共 0 条")
        self.search_status.setMinimumWidth(150)
        self.search_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        search_row.addWidget(search_label)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_status)
        root.addLayout(search_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.record_list = QListWidget()
        self.record_list.setMinimumWidth(350)
        self.record_list.currentRowChanged.connect(self._show_entry)
        splitter.addWidget(self.record_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.preview = QLabel("选择左侧记录查看确认画面")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(500, 380)
        self.preview.setStyleSheet("background: #20242b; color: #d7dde5; border: 1px solid #c8d0da;")
        right_layout.addWidget(self.preview, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(190)
        right_layout.addWidget(self.details)
        splitter.addWidget(right)
        splitter.setSizes([370, 810])
        root.addWidget(splitter, 1)

        actions = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        open_folder = QPushButton("打开记录文件夹")
        open_folder.clicked.connect(self._open_folder)
        actions.addWidget(refresh)
        actions.addWidget(open_folder)
        actions.addStretch()
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_buttons.rejected.connect(self.reject)
        actions.addWidget(close_buttons)
        root.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        self.all_entries = self.service.list_entries()
        self._apply_filter(self.search_edit.text())

    def _apply_filter(self, pattern: str) -> None:
        try:
            self.entries = self.service.filter_entries(self.all_entries, pattern)
        except re.error as exc:
            self.entries = []
            self.search_edit.setStyleSheet("border: 1px solid #d92d20;")
            self.search_status.setStyleSheet("color: #d92d20;")
            self.search_status.setText(f"正则无效：{exc.msg}")
            self.search_status.setToolTip(str(exc))
            self._populate_list("正则表达式无效，请修改搜索内容。")
            return

        self.search_edit.setStyleSheet("")
        self.search_status.setStyleSheet("")
        self.search_status.setToolTip("")
        if pattern.strip():
            self.search_status.setText(f"找到 {len(self.entries)} / {len(self.all_entries)} 条")
            empty_message = "没有匹配的历史记录，请调整搜索条件。"
        else:
            self.search_status.setText(f"共 {len(self.all_entries)} 条")
            empty_message = "还没有框选历史记录"
        self._populate_list(empty_message)

    def _populate_list(self, empty_message: str) -> None:
        self.record_list.blockSignals(True)
        self.record_list.clear()
        for entry in self.entries:
            item = QListWidgetItem(
                f"{entry.timestamp.replace('T', ' ')}  ·  {entry.event_type}\n"
                f"{entry.proposed_filename or entry.file_path}"
            )
            item.setToolTip(entry.file_path)
            self.record_list.addItem(item)
        self.record_list.blockSignals(False)
        if self.entries:
            self.record_list.setCurrentRow(0)
        else:
            self.current_pixmap = None
            self.preview.clear()
            self.preview.setText(empty_message)
            if self.all_entries:
                self.details.setPlainText("搜索范围：物料编码、名称、工序/工艺编码；三项中任意一项匹配即可。")
            else:
                self.details.setPlainText("点击“确认并下一份”后，会自动保存现场画面和框选数据。")

    def _show_entry(self, row: int) -> None:
        if not 0 <= row < len(self.entries):
            return
        entry = self.entries[row]
        self.current_pixmap = QPixmap(str(entry.screenshot_path)) if entry.screenshot_path.is_file() else None
        self._update_preview()
        field_lines = []
        for key in ("material", "name", "process"):
            field = entry.fields.get(key, {})
            confidence = field.get("confidence")
            confidence_text = "-" if confidence is None else f"{float(confidence):.1%}"
            field_lines.append(
                f"{field.get('label', key)}：{field.get('text', '')}  "
                f"（OCR {confidence_text}，人工修改：{'是' if field.get('manually_edited') else '否'}）"
            )
        self.details.setPlainText(
            "\n".join(
                [
                    f"记录类型：{entry.event_type}",
                    f"确认时间：{entry.timestamp.replace('T', ' ')}",
                    f"原始路径：{entry.original_path}",
                    f"确认时路径：{entry.file_path}",
                    f"拟定文件名：{entry.proposed_filename}",
                    f"图纸旋转：{entry.rotation}°",
                    *field_lines,
                    f"框选数据：{entry.json_path}",
                ]
            )
        )

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.service.ensure_root())))

    def _update_preview(self) -> None:
        if self.current_pixmap is None or self.current_pixmap.isNull():
            self.preview.clear()
            self.preview.setText("确认画面不存在")
            return
        self.preview.setPixmap(
            self.current_pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._update_preview()
