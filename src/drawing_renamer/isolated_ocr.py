from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

from .models import FieldKind, NormalizedRect
from .ocr_service import OcrUnavailableError, SuggestionResult


logger = logging.getLogger("drawing_renamer.ocr_process")


class OcrCancelledError(OcrUnavailableError):
    pass


class IsolatedOcrService:
    """Run native OCR in a child process so a native crash cannot kill the GUI."""

    def __init__(self, recognition_timeout_seconds: int = 20, suggestion_timeout_seconds: int = 120) -> None:
        self.recognition_timeout_seconds = recognition_timeout_seconds
        self.suggestion_timeout_seconds = suggestion_timeout_seconds
        self._lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._cancel_requested = threading.Event()

    def cancel_current(self) -> bool:
        self._cancel_requested.set()
        with self._lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            logger.info("Terminating OCR worker on user request: pid=%s", process.pid)
            process.kill()
            return True
        return False

    def recognize_text(self, image: Image.Image) -> tuple[str, float | None]:
        payload = self._run("recognize", image)
        confidence = payload.get("confidence")
        return str(payload.get("text", "")), float(confidence) if confidence is not None else None

    def suggest(self, image: Image.Image) -> SuggestionResult:
        payload = self._run("suggest", image)
        boxes = {
            FieldKind(key): NormalizedRect(**value)
            for key, value in payload.get("boxes", {}).items()
        }
        recognized = {
            FieldKind(key): (str(value[0]), float(value[1]))
            for key, value in payload.get("recognized", {}).items()
        }
        return SuggestionResult(
            rotation=int(payload.get("rotation", 0)),
            boxes=boxes,
            recognized=recognized,
            anchor_found=bool(payload.get("anchor_found", False)),
            message=str(payload.get("message", "")),
        )

    def _run(self, mode: str, image: Image.Image) -> dict[str, object]:
        if self._cancel_requested.is_set():
            self._cancel_requested.clear()
            raise OcrCancelledError("OCR任务已取消")
        with tempfile.TemporaryDirectory(prefix="drawing_renamer_ocr_") as temp:
            temp_path = Path(temp)
            input_path = temp_path / "input.png"
            output_path = temp_path / "result.json"
            crash_path = temp_path / "worker_crash.log"
            image.convert("RGB").save(input_path, "PNG")

            executable = Path(sys.executable)
            if executable.name.lower() == "pythonw.exe":
                console_python = executable.with_name("python.exe")
                if console_python.exists():
                    executable = console_python

            command = [
                str(executable),
                "-m",
                "drawing_renamer.ocr_worker",
                mode,
                str(input_path),
                str(output_path),
                str(crash_path),
            ]
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            logger.info("Starting isolated OCR worker: mode=%s image=%sx%s", mode, image.width, image.height)
            started_at = time.perf_counter()
            timeout_seconds = (
                self.recognition_timeout_seconds if mode == "recognize" else self.suggestion_timeout_seconds
            )
            process: subprocess.Popen[str] | None = None
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    creationflags=creation_flags,
                )
                with self._lock:
                    self._active_process = process
                if self._cancel_requested.is_set():
                    process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    stdout, stderr = process.communicate()
                    logger.error("OCR worker timed out after %ss: mode=%s", timeout_seconds, mode)
                    raise OcrUnavailableError(
                        f"OCR超过{timeout_seconds}秒未完成，已自动终止。可调整框选范围后重试或手工输入。"
                    ) from exc
            finally:
                with self._lock:
                    self._active_process = None

            if process is None:
                raise OcrUnavailableError("无法启动OCR独立进程")
            if self._cancel_requested.is_set():
                self._cancel_requested.clear()
                raise OcrCancelledError("OCR任务已取消")

            if stdout.strip():
                logger.info("OCR worker stdout: %s", stdout.strip()[-2000:])
            if stderr.strip():
                logger.warning("OCR worker stderr: %s", stderr.strip()[-4000:])

            crash_text = ""
            if crash_path.exists():
                crash_text = crash_path.read_text(encoding="utf-8", errors="replace").strip()
            if process.returncode != 0:
                logger.error(
                    "OCR worker crashed/failed: mode=%s returncode=%s crash=%s",
                    mode,
                    process.returncode,
                    crash_text[-4000:],
                )
                raise OcrUnavailableError(
                    "OCR独立进程异常退出，主程序已受到保护。"
                    f"错误代码：{process.returncode}。请通过“问题反馈日志”导出日志。"
                )
            logger.info(
                "Isolated OCR worker finished: mode=%s elapsed=%.2fs",
                mode,
                time.perf_counter() - started_at,
            )
            if not output_path.exists():
                raise OcrUnavailableError("OCR进程未返回识别结果")

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            if not payload.get("ok", False):
                raise OcrUnavailableError(str(payload.get("error", "OCR识别失败")))
            result = payload.get("result")
            if not isinstance(result, dict):
                raise OcrUnavailableError("OCR返回结果格式错误")
            return result
