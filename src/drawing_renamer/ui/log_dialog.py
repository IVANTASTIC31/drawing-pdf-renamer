from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..diagnostics import DiagnosticsContext, export_feedback_bundle, tail_logs


class LogDialog(QDialog):
    def __init__(self, diagnostics: DiagnosticsContext, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.diagnostics = diagnostics
        self.setWindowTitle("问题反馈日志")
        self.resize(960, 650)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "如果软件闪退，请重新打开软件后进入这里，将反馈压缩包发给开发人员。"
            "日志记录文件路径、操作阶段和异常信息，不包含PDF图纸内容。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        path_label = QLabel(f"日志目录：{diagnostics.log_file.parent}")
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_label)

        note_label = QLabel("问题描述（可选）：")
        layout.addWidget(note_label)
        self.user_note = QPlainTextEdit()
        self.user_note.setPlaceholderText("例如：选择某个文件夹后一直加载，约30秒后闪退。")
        self.user_note.setMaximumHeight(85)
        layout.addWidget(self.user_note)

        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.viewer, 1)

        buttons = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        copy_path = QPushButton("复制日志目录")
        copy_path.clicked.connect(
            lambda: QApplication.clipboard().setText(str(self.diagnostics.log_file.parent))
        )
        export = QPushButton("导出反馈压缩包")
        export.clicked.connect(self.export_bundle)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(refresh)
        buttons.addWidget(copy_path)
        buttons.addStretch()
        buttons.addWidget(export)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.viewer.setPlainText(tail_logs(self.diagnostics))
        cursor = self.viewer.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.viewer.setTextCursor(cursor)

    def export_bundle(self) -> None:
        default_name = Path.home() / "Desktop" / "PDF重命名器反馈日志.zip"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "导出反馈日志",
            str(default_name),
            "ZIP 压缩包 (*.zip)",
        )
        if not filename:
            return
        try:
            output = export_feedback_bundle(
                self.diagnostics,
                Path(filename),
                self.user_note.toPlainText(),
            )
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", f"反馈日志已保存：\n{output}")
