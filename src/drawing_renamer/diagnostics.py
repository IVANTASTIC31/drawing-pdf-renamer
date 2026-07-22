from __future__ import annotations

import faulthandler
import logging
import os
import platform
import sys
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER_NAME = "drawing_renamer"
_crash_stream = None
_original_excepthook = sys.excepthook


@dataclass(slots=True)
class DiagnosticsContext:
    data_directory: Path
    log_file: Path
    crash_file: Path
    session_marker: Path
    previous_unclean_exit: bool


def app_data_directory() -> Path:
    override = os.environ.get("DRAWING_RENAMER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "DrawingPdfRenamer"
    return Path.home() / ".drawing_pdf_renamer"


def setup_diagnostics() -> DiagnosticsContext:
    global _crash_stream

    data_directory = app_data_directory()
    log_directory = data_directory / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_file = log_directory / "app.log"
    crash_file = log_directory / "native_crash.log"
    session_marker = data_directory / "session.running"
    previous_unclean_exit = session_marker.exists()

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_file,
            maxBytes=2 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(threadName)s | %(name)s | %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)

    session_marker.write_text(
        f"pid={os.getpid()}\nstarted={datetime.now().isoformat()}\n",
        encoding="utf-8",
    )

    try:
        _crash_stream = crash_file.open("a", encoding="utf-8")
        _crash_stream.write(f"\n--- session {datetime.now().isoformat()} pid={os.getpid()} ---\n")
        _crash_stream.flush()
        faulthandler.enable(_crash_stream, all_threads=True)
    except OSError:
        _crash_stream = None

    def exception_hook(exc_type, exc_value, exc_traceback) -> None:  # type: ignore[no-untyped-def]
        logger.critical(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        _original_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = exception_hook
    if hasattr(threading, "excepthook"):
        original_thread_hook = threading.excepthook

        def thread_hook(args) -> None:  # type: ignore[no-untyped-def]
            logger.critical(
                "Uncaught thread exception in %s",
                args.thread.name if args.thread else "unknown",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            original_thread_hook(args)

        threading.excepthook = thread_hook

    logger.info("=" * 70)
    logger.info("Application starting; pid=%s", os.getpid())
    logger.info("Platform=%s", platform.platform())
    logger.info("Python=%s", sys.version.replace("\n", " "))
    logger.info("WorkingDirectory=%s", Path.cwd())
    if previous_unclean_exit:
        logger.warning("Previous session marker still exists; previous run may have exited abnormally")

    return DiagnosticsContext(
        data_directory,
        log_file,
        crash_file,
        session_marker,
        previous_unclean_exit,
    )


def mark_clean_exit(context: DiagnosticsContext) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.info("Application exited normally")
    try:
        context.session_marker.unlink(missing_ok=True)
    except OSError:
        logger.exception("Unable to remove session marker")
    for handler in logger.handlers:
        handler.flush()


def tail_logs(context: DiagnosticsContext, max_chars: int = 120_000) -> str:
    sections: list[str] = []
    candidates = [context.log_file, context.crash_file]
    for path in candidates:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            sections.append(f"===== {path.name} =====\n{content[-max_chars:]}")
        except OSError as exc:
            sections.append(f"===== {path.name} =====\n读取失败：{exc}")
    return "\n\n".join(sections) or "当前还没有日志内容。"


def export_feedback_bundle(
    context: DiagnosticsContext,
    destination: Path,
    user_note: str = "",
) -> Path:
    destination = destination.with_suffix(".zip")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(context.log_file.parent.glob("app.log*")):
            if path.is_file():
                archive.write(path, f"logs/{path.name}")
        if context.crash_file.is_file():
            archive.write(context.crash_file, f"logs/{context.crash_file.name}")
        info = context.data_directory / "system_info.txt"
        info.write_text(
            "\n".join(
                [
                    f"exported={datetime.now().isoformat()}",
                    f"platform={platform.platform()}",
                    f"python={sys.version}",
                    f"executable={sys.executable}",
                ]
            ),
            encoding="utf-8",
        )
        archive.write(info, "system_info.txt")
        info.unlink(missing_ok=True)
        if user_note.strip():
            note = context.data_directory / "user_feedback.txt"
            note.write_text(user_note.strip(), encoding="utf-8")
            archive.write(note, "user_feedback.txt")
            note.unlink(missing_ok=True)
    return destination
