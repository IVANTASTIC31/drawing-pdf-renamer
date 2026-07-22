from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import DocumentStatus, DrawingDocument
from .naming import validate_destination


@dataclass(slots=True)
class RenameResult:
    source: Path
    destination: Path | None
    success: bool
    message: str


class RenameService:
    def validate_batch(self, documents: list[DrawingDocument]) -> list[str]:
        errors: list[str] = []
        reserved: set[Path] = set()
        for document in documents:
            if document.status != DocumentStatus.CONFIRMED:
                errors.append(f"{document.path.name} 尚未人工确认")
                continue
            filename = document.confirmed_filename
            problem = validate_destination(document.path, filename, reserved)
            if problem:
                errors.append(f"{document.path.name}：{problem}")
            reserved.add(document.path.with_name(filename))
        return errors

    def execute(self, documents: list[DrawingDocument], log_directory: Path) -> list[RenameResult]:
        errors = self.validate_batch(documents)
        if errors:
            raise ValueError("\n".join(errors))

        results: list[RenameResult] = []
        log_directory.mkdir(parents=True, exist_ok=True)
        for document in documents:
            source = document.path
            destination = source.with_name(document.confirmed_filename)
            try:
                if source != destination:
                    source.rename(destination)
                document.path = destination
                document.renamed_path = destination
                document.status = DocumentStatus.RENAMED
                results.append(RenameResult(source, destination, True, "完成"))
            except OSError as exc:
                document.status = DocumentStatus.ERROR
                document.error = str(exc)
                results.append(RenameResult(source, None, False, str(exc)))

        self._write_log(log_directory, results)
        return results

    def execute_one(
        self,
        document: DrawingDocument,
        filename: str,
        log_directory: Path,
    ) -> RenameResult:
        source = document.path
        if not source.is_file():
            raise ValueError(f"文件不存在或已被移动：{source}")
        destination = source.with_name(filename)
        if destination == source:
            raise ValueError("新文件名与当前文件名相同，无需重新命名")
        problem = validate_destination(source, filename)
        if problem:
            raise ValueError(problem)
        log_directory.mkdir(parents=True, exist_ok=True)
        try:
            source.rename(destination)
            document.path = destination
            document.renamed_path = destination
            document.confirmed_filename = filename
            document.status = DocumentStatus.RENAMED
            document.error = ""
            result = RenameResult(source, destination, True, "单文件修正完成")
        except OSError as exc:
            document.status = DocumentStatus.ERROR
            document.error = str(exc)
            result = RenameResult(source, None, False, str(exc))
        self._write_log(log_directory, [result], prefix="single_rename_log")
        return result

    @staticmethod
    def _write_log(
        log_directory: Path,
        results: list[RenameResult],
        prefix: str = "rename_log",
    ) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = log_directory / f"{prefix}_{timestamp}.csv"
        with log_path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["原文件", "新文件", "结果", "说明"])
            for result in results:
                writer.writerow(
                    [
                        str(result.source),
                        str(result.destination or ""),
                        "成功" if result.success else "失败",
                        result.message,
                    ]
                )
        return log_path
