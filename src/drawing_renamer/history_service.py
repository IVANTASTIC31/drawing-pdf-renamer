from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtGui import QImage

from .models import DrawingDocument, FieldKind


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    record_id: str
    timestamp: str
    event_type: str
    original_path: str
    file_path: str
    proposed_filename: str
    rotation: int
    boxes: dict[str, dict[str, float]]
    fields: dict[str, dict[str, Any]]
    screenshot_path: Path
    json_path: Path


class HistoryService:
    SCHEMA_VERSION = 1

    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def create_payload(self, document: DrawingDocument, event_type: str) -> dict[str, Any]:
        now = datetime.now()
        record_id = now.strftime("%Y%m%d_%H%M%S_%f")
        return {
            "schema_version": self.SCHEMA_VERSION,
            "record_id": record_id,
            "timestamp": now.isoformat(timespec="seconds"),
            "event_type": event_type,
            "original_path": str(document.original_path or document.path),
            "file_path": str(document.path),
            "current_filename": document.path.name,
            "proposed_filename": document.proposed_filename,
            "confirmed_filename": document.confirmed_filename,
            "rotation": document.rotation,
            "boxes": {
                kind.value: {
                    "label": kind.label,
                    "x": rect.x,
                    "y": rect.y,
                    "width": rect.width,
                    "height": rect.height,
                }
                for kind, rect in document.boxes.items()
            },
            "fields": {
                kind.value: {
                    "label": kind.label,
                    "text": document.fields[kind].text,
                    "confidence": document.fields[kind].confidence,
                    "manually_edited": document.fields[kind].manually_edited,
                }
                for kind in FieldKind
            },
        }

    def save(self, payload: dict[str, Any], screenshot: QImage) -> HistoryEntry:
        timestamp = str(payload["timestamp"])
        day_directory = self.root / timestamp[:10].replace("-", "")
        day_directory.mkdir(parents=True, exist_ok=True)
        record_id = str(payload["record_id"])
        source_stem = Path(str(payload["file_path"])).stem
        safe_stem = re.sub(r'[<>:"/\\|?*]+', "-", source_stem).strip(" .")[:60] or "document"
        base_name = f"{record_id}_{safe_stem}"
        screenshot_path = day_directory / f"{base_name}_确认画面.png"
        json_path = day_directory / f"{base_name}_框选数据.json"

        if not screenshot.save(str(screenshot_path), "PNG"):
            raise OSError(f"无法保存历史截图：{screenshot_path}")
        stored_payload = dict(payload)
        stored_payload["screenshot"] = screenshot_path.name
        temporary_path = json_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(stored_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(json_path)
        return self._entry_from_payload(stored_payload, json_path)

    def list_entries(self) -> list[HistoryEntry]:
        entries: list[HistoryEntry] = []
        for json_path in self.root.rglob("*_框选数据.json"):
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                entries.append(self._entry_from_payload(payload, json_path))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(entries, key=lambda entry: entry.timestamp, reverse=True)

    @staticmethod
    def _entry_from_payload(payload: dict[str, Any], json_path: Path) -> HistoryEntry:
        return HistoryEntry(
            record_id=str(payload["record_id"]),
            timestamp=str(payload["timestamp"]),
            event_type=str(payload.get("event_type", "确认")),
            original_path=str(payload.get("original_path", "")),
            file_path=str(payload.get("file_path", "")),
            proposed_filename=str(payload.get("proposed_filename", "")),
            rotation=int(payload.get("rotation", 0)),
            boxes=dict(payload.get("boxes", {})),
            fields=dict(payload.get("fields", {})),
            screenshot_path=json_path.parent / str(payload.get("screenshot", "")),
            json_path=json_path,
        )
