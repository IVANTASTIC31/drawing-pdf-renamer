from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..update_service import UpdateInfo


class UpdateDialog(QDialog):
    def __init__(self, current_version: str, info: UpdateInfo, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.info = info
        self.setWindowTitle("发现软件更新")
        self.resize(650, 480)

        layout = QVBoxLayout(self)
        title = QLabel(f"发现新版本 v{info.version}")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #1677ff;")
        layout.addWidget(title)

        summary = QHBoxLayout()
        summary.addWidget(QLabel(f"当前版本：v{current_version}"))
        summary.addSpacing(24)
        summary.addWidget(QLabel(f"安装包：{_format_size(info.asset.size)}"))
        summary.addStretch()
        layout.addLayout(summary)

        layout.addWidget(QLabel("更新说明"))
        notes = QTextBrowser()
        notes.setPlainText(info.notes)
        notes.setOpenExternalLinks(True)
        layout.addWidget(notes, 1)

        privacy = QLabel("更新过程只访问 GitHub Release，不会上传PDF、识别内容、历史记录或日志。")
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #667085;")
        layout.addWidget(privacy)

        buttons = QDialogButtonBox()
        release_button = QPushButton("查看发布页面")
        release_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(info.release_url))
        )
        buttons.addButton(release_button, QDialogButtonBox.ButtonRole.ActionRole)
        later_button = buttons.addButton("暂不更新", QDialogButtonBox.ButtonRole.RejectRole)
        update_button = buttons.addButton("下载并更新", QDialogButtonBox.ButtonRole.AcceptRole)
        update_button.setDefault(True)
        update_button.setStyleSheet(
            "QPushButton { background: #1677ff; color: white; font-weight: 700; padding: 8px 18px; }"
        )
        later_button.setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons, alignment=Qt.AlignmentFlag.AlignRight)


def _format_size(size: int) -> str:
    if size <= 0:
        return "未知"
    return f"{size / (1024 * 1024):.1f} MB"
