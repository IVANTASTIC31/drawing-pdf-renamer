from __future__ import annotations

from pathlib import Path


def is_pdf_file(path: Path) -> bool:
    """Return True only for an existing regular PDF file, never a `.pdf` directory."""

    return path.suffix.lower() == ".pdf" and path.is_file()


def discover_pdfs(folder: Path) -> list[Path]:
    """Recursively find actual PDF files below a selected folder."""

    if not folder.is_dir():
        return []
    return sorted(
        (path.resolve() for path in folder.rglob("*") if is_pdf_file(path)),
        key=lambda path: str(path).casefold(),
    )
