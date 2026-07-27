from __future__ import annotations

import hashlib
import io
import json
import ssl
import urllib.error
import zipfile
from pathlib import Path

import pytest

from drawing_renamer import update_service
from drawing_renamer.update_service import (
    PreparedUpdate,
    ReleaseAsset,
    UpdateError,
    UpdateInfo,
    UpdateService,
    parse_version,
)


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, content_length: int | None = None) -> None:
        super().__init__(data)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _info(archive: bytes, edition: str = "portable") -> UpdateInfo:
    name = f"DrawingPdfRenamer-v0.1.3-windows-{edition}.zip"
    return UpdateInfo(
        version="0.1.3",
        tag_name="v0.1.3",
        notes="更新说明",
        release_url="https://github.com/example/release",
        published_at="2026-07-27T00:00:00Z",
        asset=ReleaseAsset(
            name=name,
            download_url="https://example.invalid/update.zip",
            size=len(archive),
            sha256=hashlib.sha256(archive).hexdigest(),
        ),
    )


def test_parse_version_accepts_release_tag_and_rejects_invalid_value() -> None:
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("1.2.3") == (1, 2, 3)
    with pytest.raises(ValueError):
        parse_version("1.2")


def test_ssl_context_combines_system_and_bundled_ca(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ca_file = tmp_path / "cacert.pem"
    ca_file.write_text("test", encoding="ascii")
    loaded: list[str] = []

    class FakeContext:
        def load_verify_locations(self, *, cafile: str) -> None:
            loaded.append(cafile)

    update_service._ssl_context.cache_clear()
    monkeypatch.setattr(update_service.ssl, "create_default_context", FakeContext)
    monkeypatch.setattr(update_service.certifi, "where", lambda: str(ca_file))

    assert update_service._ssl_context() is not None
    assert loaded == [str(ca_file)]
    update_service._ssl_context.cache_clear()


def test_open_url_uses_combined_ssl_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sentinel = object()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(update_service, "_ssl_context", lambda: sentinel)
    monkeypatch.setattr(
        update_service.urllib.request,
        "urlopen",
        lambda request, **kwargs: calls.append(kwargs),
    )

    update_service._open_url(
        update_service.urllib.request.Request("https://example.invalid"),
        7.0,
    )

    assert calls == [{"timeout": 7.0, "context": sentinel}]


def test_certificate_error_has_actionable_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    certificate_error = ssl.SSLCertVerificationError(
        1,
        "unable to get local issuer certificate",
    )
    monkeypatch.setattr(
        update_service,
        "_open_url",
        lambda request, timeout: (_ for _ in ()).throw(
            urllib.error.URLError(certificate_error)
        ),
    )

    with pytest.raises(UpdateError, match="Windows根证书"):
        update_service._request_bytes("https://example.invalid")


def test_check_selects_matching_edition_and_digest(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    digest = "a" * 64
    payload = {
        "tag_name": "v0.1.3",
        "body": "修复内容",
        "html_url": "https://github.com/example/release",
        "published_at": "2026-07-27T00:00:00Z",
        "assets": [
            {
                "name": "DrawingPdfRenamer-v0.1.3-windows-portable.zip",
                "browser_download_url": "https://example.invalid/portable.zip",
                "size": 123,
                "digest": f"sha256:{digest}",
            }
        ],
    }
    monkeypatch.setattr(
        update_service,
        "_open_url",
        lambda request, timeout: FakeResponse(json.dumps(payload).encode("utf-8")),
    )

    info = UpdateService("example/repo").check("0.1.2", "portable")

    assert info is not None
    assert info.version == "0.1.3"
    assert info.asset.sha256 == digest
    assert info.asset.size == 123


def test_check_returns_none_when_current_version_is_latest(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = {"tag_name": "v0.1.2", "assets": []}
    monkeypatch.setattr(
        update_service,
        "_open_url",
        lambda request, timeout: FakeResponse(json.dumps(payload).encode("utf-8")),
    )

    assert UpdateService("example/repo").check("0.1.2", "portable") is None


def test_download_verifies_hash_and_extracts_portable_package(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    archive = _zip_bytes(
        {
            "DrawingPdfRenamer.exe": b"binary",
            "_internal/library.bin": b"dependency",
        }
    )
    monkeypatch.setattr(
        update_service,
        "_open_url",
        lambda request, timeout: FakeResponse(archive, len(archive)),
    )
    progress: list[tuple[int, int]] = []

    prepared = UpdateService().download_and_prepare(
        _info(archive),
        tmp_path,
        lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert prepared.archive_path.is_file()
    assert (prepared.staging_directory / "DrawingPdfRenamer.exe").read_bytes() == b"binary"
    assert (prepared.staging_directory / "_internal" / "library.bin").is_file()
    assert progress[-1] == (len(archive), len(archive))


def test_download_rejects_hash_mismatch(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    archive = _zip_bytes({"DrawingPdfRenamer.exe": b"binary"})
    info = _info(archive)
    broken_info = UpdateInfo(
        info.version,
        info.tag_name,
        info.notes,
        info.release_url,
        info.published_at,
        ReleaseAsset(
            info.asset.name,
            info.asset.download_url,
            info.asset.size,
            "0" * 64,
        ),
    )
    monkeypatch.setattr(
        update_service,
        "_open_url",
        lambda request, timeout: FakeResponse(archive, len(archive)),
    )

    with pytest.raises(UpdateError, match="SHA256"):
        UpdateService().download_and_prepare(broken_info, tmp_path)


def test_safe_extract_rejects_parent_directory_member(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    archive_path.write_bytes(_zip_bytes({"../outside.txt": b"no"}))

    with pytest.raises(UpdateError, match="不安全"):
        UpdateService._extract_safely(archive_path, tmp_path / "staging")


def test_portable_installer_is_started_hidden(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "DrawingPdfRenamer"
    target.mkdir()
    executable = target / "DrawingPdfRenamer.exe"
    executable.write_bytes(b"old")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / executable.name).write_bytes(b"new")
    info = _info(_zip_bytes({"DrawingPdfRenamer.exe": b"new"}))
    prepared = PreparedUpdate(info, tmp_path / info.asset.name, staging)
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(update_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update_service.sys, "executable", str(executable))
    monkeypatch.setattr(
        update_service.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    UpdateService().launch_portable_installer(prepared, tmp_path / "data")

    assert calls
    command, options = calls[0]
    assert command[0] == "powershell.exe"
    assert "-OldPid" in command
    assert "-Staging" in command
    assert "-CleanupDirectory" in command
    assert options["cwd"] == str(tmp_path / "data" / "updates" / "installer")
    assert (tmp_path / "data" / "updates" / "installer" / "apply-update.ps1").is_file()


def test_portable_installer_rejects_install_folder_containing_user_data(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "DrawingPdfRenamer"
    target.mkdir()
    executable = target / "DrawingPdfRenamer.exe"
    executable.write_bytes(b"old")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / executable.name).write_bytes(b"new")
    info = _info(_zip_bytes({"DrawingPdfRenamer.exe": b"new"}))
    prepared = PreparedUpdate(info, tmp_path / info.asset.name, staging)

    monkeypatch.setattr(update_service.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update_service.sys, "executable", str(executable))

    with pytest.raises(UpdateError, match="用户数据"):
        UpdateService().launch_portable_installer(prepared, target / "history-and-logs")
