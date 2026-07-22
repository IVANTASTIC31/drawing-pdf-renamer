from pathlib import Path
from zipfile import ZipFile

from drawing_renamer.diagnostics import DiagnosticsContext, export_feedback_bundle, tail_logs


def context(tmp_path: Path) -> DiagnosticsContext:
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    log_file = log_directory / "app.log"
    crash_file = log_directory / "native_crash.log"
    log_file.write_text("normal log line", encoding="utf-8")
    crash_file.write_text("native crash line", encoding="utf-8")
    return DiagnosticsContext(tmp_path, log_file, crash_file, tmp_path / "session.running", False)


def test_tail_logs_contains_normal_and_native_logs(tmp_path: Path) -> None:
    content = tail_logs(context(tmp_path))
    assert "normal log line" in content
    assert "native crash line" in content


def test_feedback_bundle_contains_logs_and_system_info(tmp_path: Path) -> None:
    destination = export_feedback_bundle(context(tmp_path), tmp_path / "feedback", "闪退前正在打开文件夹")
    with ZipFile(destination) as archive:
        names = set(archive.namelist())
        note = archive.read("user_feedback.txt").decode("utf-8")
    assert "logs/app.log" in names
    assert "logs/native_crash.log" in names
    assert "system_info.txt" in names
    assert "闪退" in note
