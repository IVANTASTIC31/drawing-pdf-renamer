from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FieldKind(str, Enum):
    MATERIAL = "material"
    NAME = "name"
    PROCESS = "process"

    @property
    def label(self) -> str:
        return {
            FieldKind.MATERIAL: "物料编码",
            FieldKind.NAME: "名称",
            FieldKind.PROCESS: "工序编号",
        }[self]


class DocumentStatus(str, Enum):
    PENDING = "待处理"
    NEEDS_BOXES = "待框选"
    OCR_QUEUED = "等待识别"
    OCR_RUNNING = "识别中"
    NEEDS_CONFIRMATION = "待确认"
    CONFIRMED = "已确认"
    RENAMED = "已重命名"
    ERROR = "异常"


@dataclass(slots=True)
class NormalizedRect:
    """Rectangle in image coordinates, normalized to 0..1."""

    x: float
    y: float
    width: float
    height: float

    def clamped(self) -> "NormalizedRect":
        x = min(max(self.x, 0.0), 1.0)
        y = min(max(self.y, 0.0), 1.0)
        width = min(max(self.width, 0.0), 1.0 - x)
        height = min(max(self.height, 0.0), 1.0 - y)
        return NormalizedRect(x, y, width, height)


@dataclass(slots=True)
class FieldValue:
    text: str = ""
    confidence: float | None = None
    manually_edited: bool = False
    message: str = ""


@dataclass(slots=True)
class DrawingDocument:
    path: Path
    original_path: Path | None = None
    rotation: int = 0
    boxes: dict[FieldKind, NormalizedRect] = field(default_factory=dict)
    fields: dict[FieldKind, FieldValue] = field(
        default_factory=lambda: {kind: FieldValue() for kind in FieldKind}
    )
    status: DocumentStatus = DocumentStatus.PENDING
    error: str = ""
    proposed_filename: str = ""
    confirmed_filename: str = ""
    renamed_path: Path | None = None
    ocr_revision: int = 0
    ocr_progress: int = 0

    def __post_init__(self) -> None:
        if self.original_path is None:
            self.original_path = self.path

    @property
    def display_name(self) -> str:
        return self.path.name

    @property
    def all_fields_filled(self) -> bool:
        return all(self.fields[kind].text.strip() for kind in FieldKind)

    @property
    def all_boxes_present(self) -> bool:
        return all(kind in self.boxes for kind in FieldKind)
