from pathlib import Path

from drawing_renamer.file_discovery import discover_pdfs, is_pdf_file


def test_pdf_named_directory_is_not_treated_as_file(tmp_path: Path) -> None:
    misleading = tmp_path / "drawing.pdf"
    misleading.mkdir()
    real_pdf = misleading / "page-01.pdf"
    real_pdf.write_bytes(b"pdf")

    assert not is_pdf_file(misleading)
    assert discover_pdfs(tmp_path) == [real_pdf.resolve()]


def test_discovery_is_recursive_and_case_insensitive(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    pdf = nested / "A.PDF"
    pdf.write_bytes(b"pdf")
    (nested / "note.txt").write_text("ignore", encoding="utf-8")

    assert discover_pdfs(tmp_path) == [pdf.resolve()]
