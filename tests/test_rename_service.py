from pathlib import Path

from drawing_renamer.models import DocumentStatus, DrawingDocument
from drawing_renamer.rename_service import RenameService


def confirmed(source: Path, filename: str) -> DrawingDocument:
    document = DrawingDocument(source)
    document.status = DocumentStatus.CONFIRMED
    document.confirmed_filename = filename
    return document


def test_duplicate_batch_name_is_rejected(tmp_path: Path) -> None:
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    documents = [confirmed(first, "same.pdf"), confirmed(second, "same.pdf")]
    errors = RenameService().validate_batch(documents)
    assert any("重复" in error for error in errors)


def test_execute_renames_and_writes_log(tmp_path: Path) -> None:
    source = tmp_path / "old.pdf"
    source.write_bytes(b"pdf")
    document = confirmed(source, "B.001_泵体_A10.pdf")
    results = RenameService().execute([document], tmp_path / "logs")
    assert results[0].success
    destination = tmp_path / "B.001_泵体_A10.pdf"
    assert destination.exists()
    assert document.path == destination
    assert document.original_path == source
    assert list((tmp_path / "logs").glob("rename_log_*.csv"))


def test_execute_one_corrects_an_already_renamed_file(tmp_path: Path) -> None:
    source = tmp_path / "B.001_泵体_错误.pdf"
    source.write_bytes(b"pdf")
    document = DrawingDocument(source)
    document.status = DocumentStatus.RENAMED

    result = RenameService().execute_one(
        document,
        "B.001_泵体_CP41.100A.pdf",
        tmp_path / "logs",
    )

    destination = tmp_path / "B.001_泵体_CP41.100A.pdf"
    assert result.success
    assert document.path == destination
    assert document.original_path == source
    assert destination.is_file()
    assert list((tmp_path / "logs").glob("single_rename_log_*.csv"))
