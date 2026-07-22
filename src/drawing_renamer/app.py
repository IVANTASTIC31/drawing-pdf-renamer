from __future__ import annotations

import os
import sys
import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from .diagnostics import mark_clean_exit, setup_diagnostics
from .ui.main_window import MainWindow


def main() -> int:
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    diagnostics = setup_diagnostics()
    logger = logging.getLogger("drawing_renamer.app")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("工程图纸 PDF 半自动重命名")
        app.setOrganizationName("湖州三井低温设备有限公司")
        app.aboutToQuit.connect(lambda: mark_clean_exit(diagnostics))
        window = MainWindow(diagnostics)
        window.show()
        if diagnostics.previous_unclean_exit:
            QTimer.singleShot(
                400,
                lambda: QMessageBox.warning(
                    window,
                    "检测到上次异常退出",
                    "软件上次可能发生了闪退。已保留相关日志，请点击顶部“问题反馈日志”导出反馈压缩包。\n\n"
                    f"日志目录：{diagnostics.log_file.parent}",
                ),
            )
        return app.exec()
    except Exception:
        logger.exception("Fatal application startup/runtime error")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
