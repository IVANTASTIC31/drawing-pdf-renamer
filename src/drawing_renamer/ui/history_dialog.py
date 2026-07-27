from __future__ import annotations

import html
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
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
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
        self.clear_history_button = QPushButton("清除历史记录")
        self.clear_history_button.setStyleSheet(
            "QPushButton { background: #d92d20; color: white; border: 1px solid #b42318; "
            "border-radius: 5px; padding: 7px 12px; font-weight: 600; }"
            "QPushButton:hover { background: #b42318; }"
        )
        self.clear_history_button.clicked.connect(self._clear_history)
        search_row.addWidget(search_label)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.clear_history_button)
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
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(False)
        self.details.setMinimumHeight(240)
        self.details.setMaximumHeight(280)
        self.details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details.setStyleSheet(
            "QTextBrowser { background: #f8fafc; border: 1px solid #c8d0da; "
            "padding: 8px; color: #1f2937; }"
        )
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
            material = self._field_text(entry, "material")
            name = self._field_text(entry, "name")
            process = self._field_text(entry, "process")
            item = QListWidgetItem(
                f"{entry.timestamp.replace('T', ' ')}  ·  {entry.event_type}\n"
                f"物料编码：{material}\n"
                f"名称：{name}  ｜  工序编号：{process}"
            )
            item.setToolTip(
                f"拟定文件名：{entry.proposed_filename or '-'}\n"
                f"确认时路径：{entry.file_path or '-'}"
            )
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
        self.details.setHtml(self._entry_details_html(entry))

    @staticmethod
    def _field_text(entry: HistoryEntry, key: str) -> str:
        value = str(entry.fields.get(key, {}).get("text") or "").strip()
        return value or "未填写"

    def _entry_details_html(self, entry: HistoryEntry) -> str:
        cards = []
        card_styles = (
            ("material", "物料编码", "#2478ff", "#edf4ff"),
            ("name", "名称", "#16a36a", "#eaf8f2"),
            ("process", "工序编号", "#f08a24", "#fff3e8"),
        )
        for key, default_label, color, background in card_styles:
            field = entry.fields.get(key, {})
            confidence = field.get("confidence")
            confidence_text = "未记录" if confidence is None else f"{float(confidence):.1%}"
            label = html.escape(str(field.get("label") or default_label))
            value = html.escape(self._field_text(entry, key))
            edited = "是" if field.get("manually_edited") else "否"
            cards.append(
                f'<td width="33%" bgcolor="{background}" '
                f'style="border: 2px solid {color}; padding: 10px;">'
                f'<div style="color: {color}; font-size: 12px; font-weight: 700;">{label}</div>'
                f'<div style="color: #111827; font-size: 18px; font-weight: 700; '
                f'margin-top: 5px; margin-bottom: 6px;">{value}</div>'
                f'<div style="color: #667085; font-size: 10px;">'
                f'OCR置信度：{confidence_text}　人工修改：{edited}</div>'
                "</td>"
            )

        def safe(value: object, fallback: str = "-") -> str:
            text = str(value or "").strip() or fallback
            return html.escape(text)

        rows = (
            ("记录类型", safe(entry.event_type)),
            ("确认时间", safe(entry.timestamp.replace("T", " "))),
            ("拟定文件名", safe(entry.proposed_filename)),
            ("确认时路径", safe(entry.file_path)),
            ("原始路径", safe(entry.original_path)),
            ("图纸旋转", f"{entry.rotation}°"),
            ("确认时页码", f"第 {entry.page_index + 1} / {entry.page_count} 页"),
            ("框选数据", safe(entry.json_path)),
        )
        metadata = "".join(
            '<tr>'
            '<td width="90" style="color: #667085; font-weight: 600; padding: 3px 8px 3px 0;">'
            f"{label}</td>"
            '<td style="color: #344054; padding: 3px 0;">'
            f"{value}</td>"
            "</tr>"
            for label, value in rows
        )
        return (
            '<div style="font-family: Microsoft YaHei, Segoe UI, sans-serif;">'
            '<div style="color: #344054; font-size: 13px; font-weight: 700; margin-bottom: 6px;">'
            "三项识别结果</div>"
            '<table width="100%" cellspacing="8" cellpadding="0"><tr>'
            f"{''.join(cards)}"
            "</tr></table>"
            '<div style="color: #344054; font-size: 13px; font-weight: 700; '
            'margin-top: 8px; margin-bottom: 4px;">记录信息</div>'
            '<table width="100%" cellspacing="0" cellpadding="0">'
            f"{metadata}"
            "</table></div>"
        )

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.service.ensure_root())))

    def _clear_history(self) -> None:
        if not self.all_entries:
            QMessageBox.information(self, "清除历史记录", "当前没有可清除的历史记录。")
            return
        record_count = len(self.all_entries)
        answer = QMessageBox.question(
            self,
            "确认清除历史记录",
            f"即将永久删除全部 {record_count} 条历史记录，包括确认画面和框选数据。\n\n"
            "此操作无法撤销，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted_count = self.service.clear_all()
        except OSError as exc:
            QMessageBox.warning(self, "清除失败", str(exc))
            return
        self.search_edit.clear()
        self.refresh()
        QMessageBox.information(self, "清除完成", f"已清除 {deleted_count} 条历史记录。")

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
