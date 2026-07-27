from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..update_service import DownloadProgress, UpdateInfo


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
        summary.addSpacing(24)
        summary.addWidget(QLabel(f"下载源：{info.asset.source}"))
        summary.addStretch()
        layout.addLayout(summary)

        layout.addWidget(QLabel("更新说明"))
        notes = QTextBrowser()
        notes.setPlainText(info.notes)
        notes.setOpenExternalLinks(True)
        layout.addWidget(notes, 1)

        privacy = QLabel(
            "优先从 Gitee 国内镜像下载，镜像不可用时自动切换 GitHub；"
            "不会上传PDF、识别内容、历史记录或日志。"
        )
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


class UpdateDownloadDialog(QDialog):
    cancel_requested = Signal()

    def __init__(self, version: str, source: str, parent=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self.setWindowTitle(f"更新到 v{version}")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(560)
        self.setFixedHeight(290)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self._samples: deque[tuple[float, int]] = deque()
        self._last_downloaded = 0
        self._cancel_sent = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(14)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(f"正在获取 v{version}")
        title.setStyleSheet("font-size: 21px; font-weight: 700; color: #172033;")
        self.stage_label = QLabel("正在连接下载服务器…")
        self.stage_label.setStyleSheet("font-size: 13px; color: #667085;")
        heading.addWidget(title)
        heading.addWidget(self.stage_label)
        header.addLayout(heading, 1)
        self.source_label = QLabel(source)
        self.source_label.setStyleSheet(
            "QLabel { background: #eafaf4; color: #087a55; border-radius: 10px; "
            "padding: 5px 10px; font-weight: 700; }"
        )
        header.addWidget(self.source_label, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("0.0%")
        self.progress_bar.setFixedHeight(28)
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #d7e2f2; border-radius: 9px; "
            "background: #edf3fb; color: #172033; text-align: center; font-weight: 700; }"
            "QProgressBar::chunk { border-radius: 8px; "
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #2878ff,stop:1 #55a2ff); }"
        )
        layout.addWidget(self.progress_bar)

        stats = QHBoxLayout()
        self.amount_label = QLabel("已下载 0 MB")
        self.speed_label = QLabel("速度 --")
        self.eta_label = QLabel("剩余时间 --")
        for label in (self.amount_label, self.speed_label, self.eta_label):
            label.setStyleSheet("font-size: 13px; color: #344054;")
        stats.addWidget(self.amount_label)
        stats.addStretch()
        stats.addWidget(self.speed_label)
        stats.addStretch()
        stats.addWidget(self.eta_label)
        layout.addLayout(stats)

        security = QLabel("下载完成后会自动进行 SHA-256 完整性校验，再解压并安装。")
        security.setWordWrap(True)
        security.setStyleSheet(
            "QLabel { background: #f7f9fc; color: #667085; border-radius: 8px; "
            "padding: 9px 12px; }"
        )
        layout.addWidget(security)

        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.setMinimumWidth(104)
        self.cancel_button.setStyleSheet(
            "QPushButton { padding: 7px 16px; border: 1px solid #cbd5e1; "
            "border-radius: 6px; background: white; color: #344054; }"
            "QPushButton:hover { border-color: #e5484d; color: #c92a2a; }"
            "QPushButton:disabled { color: #98a2b3; background: #f2f4f7; }"
        )
        self.cancel_button.clicked.connect(self._request_cancel)
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    def reject(self) -> None:
        self._request_cancel()

    def finish_and_close(self) -> None:
        super().accept()

    def _request_cancel(self) -> None:
        if self._cancel_sent:
            return
        self._cancel_sent = True
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("正在取消…")
        self.stage_label.setText("正在停止下载，请稍候…")
        self.cancel_requested.emit()

    def update_progress(self, progress: DownloadProgress) -> None:
        self.source_label.setText(progress.source)
        if progress.stage == "verify":
            self.stage_label.setText("正在校验安装包完整性…")
            self.progress_bar.setValue(1000)
            self.progress_bar.setFormat("下载完成")
            self.speed_label.setText("SHA-256 校验中")
            self.eta_label.setText("请稍候")
            return
        if progress.stage == "extract":
            self.stage_label.setText("校验通过，正在解压并准备更新…")
            self.progress_bar.setValue(1000)
            self.progress_bar.setFormat("正在准备安装")
            self.speed_label.setText("文件完整")
            self.eta_label.setText("即将完成")
            return

        now = time.monotonic()
        if progress.downloaded < self._last_downloaded:
            self._samples.clear()
        self._last_downloaded = progress.downloaded
        self._samples.append((now, progress.downloaded))
        while len(self._samples) > 2 and now - self._samples[0][0] > 5.0:
            self._samples.popleft()

        total = progress.total
        if total > 0:
            value = min(1000, int(progress.downloaded * 1000 / total))
            percent = min(100.0, progress.downloaded * 100 / total)
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(value)
            self.progress_bar.setFormat(f"{percent:.1f}%")
            self.amount_label.setText(
                f"已下载 {_format_size(progress.downloaded)} / {_format_size(total)}"
            )
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat("")
            self.amount_label.setText(f"已下载 {_format_size(progress.downloaded)}")

        speed = 0.0
        if len(self._samples) >= 2:
            first_time, first_bytes = self._samples[0]
            elapsed = now - first_time
            if elapsed > 0:
                speed = max(0.0, (progress.downloaded - first_bytes) / elapsed)
        self.speed_label.setText(
            f"速度 {_format_speed(speed)}" if speed > 0 else "速度计算中…"
        )
        if speed > 0 and total > progress.downloaded:
            seconds = int((total - progress.downloaded) / speed)
            self.eta_label.setText(f"预计剩余 {_format_duration(seconds)}")
        elif total > 0 and progress.downloaded >= total:
            self.eta_label.setText("下载完成")
        else:
            self.eta_label.setText("剩余时间计算中…")
        self.stage_label.setText("正在下载更新包，请保持网络连接…")


def _format_size(size: int) -> str:
    if size <= 0:
        return "未知"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _format_speed(bytes_per_second: float) -> str:
    if bytes_per_second < 1024 * 1024:
        return f"{bytes_per_second / 1024:.1f} KB/s"
    return f"{bytes_per_second / (1024 * 1024):.2f} MB/s"


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{max(1, seconds)} 秒"
    minutes, remaining = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {remaining} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"
