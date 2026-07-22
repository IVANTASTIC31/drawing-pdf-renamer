from __future__ import annotations

import re
from pathlib import Path

from .models import FieldKind


INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
MULTIPLE_HYPHENS = re.compile(r"-{2,}")


def sanitize_component(value: str) -> str:
    """Make a field safe for a Windows filename without changing its meaning."""

    value = value.strip()
    value = INVALID_FILENAME_CHARS.sub("-", value)
    value = MULTIPLE_HYPHENS.sub("-", value)
    return value.rstrip(". ")


def build_filename(material: str, name: str, process: str, extension: str = ".pdf") -> str:
    parts = [sanitize_component(value) for value in (material, name, process)]
    if not all(parts):
        raise ValueError("物料编码、名称和工序编号均不能为空")
    suffix = extension if extension.startswith(".") else f".{extension}"
    return "_".join(parts) + suffix.lower()


def build_filename_from_fields(fields: dict[FieldKind, object], extension: str = ".pdf") -> str:
    def text(kind: FieldKind) -> str:
        value = fields[kind]
        return str(getattr(value, "text", value))

    return build_filename(
        text(FieldKind.MATERIAL),
        text(FieldKind.NAME),
        text(FieldKind.PROCESS),
        extension,
    )


def validate_destination(source: Path, filename: str, reserved: set[Path] | None = None) -> str | None:
    destination = source.with_name(filename)
    if destination == source:
        return None
    if destination.exists():
        return f"目标文件已存在：{destination.name}"
    if reserved and destination in reserved:
        return f"批次内存在重复文件名：{destination.name}"
    return None
