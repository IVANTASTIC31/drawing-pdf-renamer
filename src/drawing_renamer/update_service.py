from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import certifi

from .subprocess_visibility import hidden_window_options


logger = logging.getLogger("drawing_renamer.update")

REPOSITORY = "IVANTASTIC31/drawing-pdf-renamer"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "DrawingPdfRenamer-Updater",
    "X-GitHub-Api-Version": "2022-11-28",
}
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class UpdateError(RuntimeError):
    """Raised when an update cannot be checked, downloaded, or prepared."""


class UpdateCancelledError(UpdateError):
    """Raised when the user cancels an update download."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    version: str
    tag_name: str
    notes: str
    release_url: str
    published_at: str
    asset: ReleaseAsset


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    info: UpdateInfo
    archive_path: Path
    staging_directory: Path


def distribution_edition() -> str:
    return "portable" if getattr(sys, "frozen", False) else "online"


def parse_version(value: str) -> tuple[int, int, int]:
    normalized = value.strip().lower()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    core = normalized.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"不支持的版本号：{value}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Combine Windows trusted roots with the CA bundle shipped in the app."""

    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def _open_url(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())


def _request_bytes(url: str, timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with _open_url(request, timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"更新服务器返回错误：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise UpdateError(
                "HTTPS证书校验失败。请先确认电脑日期时间正确，并安装最新的Windows根证书；"
                "如果公司网络使用HTTPS代理，请联系网管确认企业根证书已安装。"
                f"详细错误：{reason}"
            ) from exc
        raise UpdateError(f"无法连接更新服务器：{reason}") from exc
    except TimeoutError as exc:
        raise UpdateError("连接更新服务器超时") from exc


def _asset_digest(asset: dict[str, object], assets: list[dict[str, object]]) -> str:
    digest = str(asset.get("digest") or "")
    if digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].strip().lower()

    checksum_asset = next(
        (
            candidate
            for candidate in assets
            if str(candidate.get("name", "")).upper() == "CHECKSUMS-SHA256.TXT"
        ),
        None,
    )
    if checksum_asset is None:
        raise UpdateError("最新版缺少 SHA256 校验信息，已拒绝下载")
    checksum_url = str(checksum_asset.get("browser_download_url") or "")
    checksum_text = _request_bytes(checksum_url).decode("utf-8", errors="replace")
    wanted_name = str(asset.get("name") or "")
    for line in checksum_text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and Path(parts[1].lstrip("*")).name == wanted_name:
            candidate = parts[0].lower()
            if len(candidate) == 64 and all(char in "0123456789abcdef" for char in candidate):
                return candidate
    raise UpdateError(f"校验文件中找不到 {wanted_name} 的 SHA256")


class UpdateService:
    def __init__(self, repository: str = REPOSITORY) -> None:
        self.repository = repository
        self.latest_release_api = f"https://api.github.com/repos/{repository}/releases/latest"

    def check(self, current_version: str, edition: str | None = None) -> UpdateInfo | None:
        edition = edition or distribution_edition()
        logger.info(
            "Checking for updates: repository=%s current=%s edition=%s",
            self.repository,
            current_version,
            edition,
        )
        try:
            payload = json.loads(_request_bytes(self.latest_release_api).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpdateError("更新服务器返回了无法解析的数据") from exc

        tag_name = str(payload.get("tag_name") or "")
        try:
            latest_version_tuple = parse_version(tag_name)
            current_version_tuple = parse_version(current_version)
        except ValueError as exc:
            raise UpdateError(str(exc)) from exc
        if latest_version_tuple <= current_version_tuple:
            logger.info("No update available: latest=%s current=%s", tag_name, current_version)
            return None

        version = ".".join(str(part) for part in latest_version_tuple)
        expected_name = f"DrawingPdfRenamer-v{version}-windows-{edition}.zip"
        assets = [item for item in payload.get("assets", []) if isinstance(item, dict)]
        asset = next((item for item in assets if item.get("name") == expected_name), None)
        if asset is None and edition == "online":
            portable_name = f"DrawingPdfRenamer-v{version}-windows-portable.zip"
            asset = next((item for item in assets if item.get("name") == portable_name), None)
            if asset is not None:
                logger.info(
                    "Online package is no longer published; using portable package: %s",
                    portable_name,
                )
                expected_name = portable_name
        if asset is None:
            raise UpdateError(
                f"最新版本尚未提供可用的 Windows 免安装包："
                f"DrawingPdfRenamer-v{version}-windows-portable.zip"
            )
        download_url = str(asset.get("browser_download_url") or "")
        if not download_url.startswith("https://"):
            raise UpdateError("最新版安装包的下载地址无效")
        digest = _asset_digest(asset, assets)
        return UpdateInfo(
            version=version,
            tag_name=tag_name,
            notes=str(payload.get("body") or "本次发布未填写更新说明。"),
            release_url=str(payload.get("html_url") or ""),
            published_at=str(payload.get("published_at") or ""),
            asset=ReleaseAsset(
                name=expected_name,
                download_url=download_url,
                size=int(asset.get("size") or 0),
                sha256=digest,
            ),
        )

    def download_and_prepare(
        self,
        info: UpdateInfo,
        root: Path,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> PreparedUpdate:
        root.mkdir(parents=True, exist_ok=True)
        version_root = root / f"v{info.version}"
        if version_root.exists():
            shutil.rmtree(version_root)
        version_root.mkdir(parents=True)
        partial_path = version_root / f"{info.asset.name}.part"
        archive_path = version_root / info.asset.name
        staging_directory = version_root / "staging"

        request = urllib.request.Request(info.asset.download_url, headers=REQUEST_HEADERS)
        downloaded = 0
        digest = hashlib.sha256()
        try:
            with _open_url(request, 30.0) as response, partial_path.open("wb") as destination:
                response_size = int(response.headers.get("Content-Length") or 0)
                total = info.asset.size or response_size
                while True:
                    if cancelled and cancelled():
                        raise UpdateCancelledError("已取消更新下载")
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    destination.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
        except UpdateCancelledError:
            partial_path.unlink(missing_ok=True)
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            partial_path.unlink(missing_ok=True)
            raise UpdateError(f"下载安装包失败：{exc}") from exc

        actual_digest = digest.hexdigest().lower()
        if actual_digest != info.asset.sha256.lower():
            partial_path.unlink(missing_ok=True)
            raise UpdateError(
                "安装包 SHA256 校验失败，文件可能不完整或已被篡改，已停止更新"
            )
        partial_path.replace(archive_path)
        self._extract_safely(archive_path, staging_directory)
        if info.asset.name.endswith("-portable.zip"):
            executable = staging_directory / "DrawingPdfRenamer.exe"
            if not executable.is_file():
                raise UpdateError("免安装版更新包结构不正确，缺少 DrawingPdfRenamer.exe")
        logger.info(
            "Update downloaded and prepared: version=%s archive=%s staging=%s",
            info.version,
            archive_path,
            staging_directory,
        )
        return PreparedUpdate(info, archive_path, staging_directory)

    @staticmethod
    def _extract_safely(archive_path: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        destination_root = destination.resolve()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    resolved = (destination / member.filename).resolve()
                    if resolved != destination_root and destination_root not in resolved.parents:
                        raise UpdateError("更新包包含不安全的文件路径，已停止解压")
                archive.extractall(destination)
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateError(f"无法解压更新包：{exc}") from exc

    def launch_portable_installer(
        self,
        prepared: PreparedUpdate,
        data_directory: Path,
    ) -> None:
        if not getattr(sys, "frozen", False):
            raise UpdateError("当前不是免安装版，不能执行便携版自动替换")
        executable = Path(sys.executable).resolve()
        target = executable.parent
        staging = prepared.staging_directory.resolve()
        data_directory = data_directory.resolve()
        if not (staging / executable.name).is_file():
            raise UpdateError("更新暂存目录中缺少主程序")
        if data_directory == target or target in data_directory.parents:
            raise UpdateError(
                "程序安装目录包含日志或历史记录目录，为避免误删用户数据，"
                "当前安装位置不支持自动替换。请手工下载新版并解压到其他目录。"
            )
        try:
            probe = target.parent / f".drawing-renamer-update-write-test-{os.getpid()}"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
        except OSError as exc:
            raise UpdateError("程序所在目录没有写入权限，无法自动更新") from exc

        updater_root = data_directory / "updates" / "installer"
        updater_root.mkdir(parents=True, exist_ok=True)
        script_path = updater_root / "apply-update.ps1"
        marker_path = updater_root / "startup-success.marker"
        log_path = updater_root / "update.log"
        marker_path.unlink(missing_ok=True)
        script_path.write_text(_POWERSHELL_UPDATER, encoding="utf-8-sig")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-OldPid",
            str(os.getpid()),
            "-Target",
            str(target),
            "-Staging",
            str(staging),
            "-ExecutableName",
            executable.name,
            "-Marker",
            str(marker_path),
            "-LogFile",
            str(log_path),
            "-CleanupDirectory",
            str(prepared.archive_path.parent),
        ]
        try:
            subprocess.Popen(
                command,
                cwd=str(updater_root),
                close_fds=True,
                **hidden_window_options(),
            )
        except OSError as exc:
            raise UpdateError(f"无法启动更新辅助程序：{exc}") from exc
        logger.info(
            "Portable update installer launched: version=%s target=%s",
            prepared.info.version,
            target,
        )


_POWERSHELL_UPDATER = r"""
param(
    [Parameter(Mandatory=$true)][int]$OldPid,
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$true)][string]$Staging,
    [Parameter(Mandatory=$true)][string]$ExecutableName,
    [Parameter(Mandatory=$true)][string]$Marker,
    [Parameter(Mandatory=$true)][string]$LogFile,
    [Parameter(Mandatory=$true)][string]$CleanupDirectory
)
$ErrorActionPreference = "Stop"

function Write-UpdateLog([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $Message"
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

$backup = "$Target.previous"
$newProcess = $null
try {
    Write-UpdateLog "Waiting for application pid=$OldPid"
    try { Wait-Process -Id $OldPid -Timeout 120 -ErrorAction SilentlyContinue } catch {}
    if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
        throw "旧程序在120秒内没有退出"
    }
    if (-not (Test-Path -LiteralPath $Staging -PathType Container)) {
        throw "更新暂存目录不存在：$Staging"
    }
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
    Move-Item -LiteralPath $Target -Destination $backup
    Move-Item -LiteralPath $Staging -Destination $Target
    Remove-Item -LiteralPath $Marker -Force -ErrorAction SilentlyContinue
    $newExe = Join-Path $Target $ExecutableName
    $newProcess = Start-Process -FilePath $newExe -ArgumentList @("--update-success-marker", $Marker) -WorkingDirectory $Target -PassThru
    Write-UpdateLog "Started new version pid=$($newProcess.Id)"

    $ready = $false
    for ($index = 0; $index -lt 120; $index++) {
        if (Test-Path -LiteralPath $Marker -PathType Leaf) {
            $ready = $true
            break
        }
        if ($newProcess.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 500
        $newProcess.Refresh()
    }
    if (-not $ready) {
        throw "新版程序没有在60秒内完成启动确认"
    }
    Remove-Item -LiteralPath $backup -Recurse -Force
    Remove-Item -LiteralPath $Marker -Force -ErrorAction SilentlyContinue
    Write-UpdateLog "Update completed successfully"
    if (Test-Path -LiteralPath $CleanupDirectory) {
        Remove-Item -LiteralPath $CleanupDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit 0
}
catch {
    Write-UpdateLog "Update failed: $($_.Exception.Message)"
    try {
        if ($newProcess -and -not $newProcess.HasExited) {
            Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $Target) {
            Remove-Item -LiteralPath $Target -Recurse -Force
        }
        if (Test-Path -LiteralPath $backup) {
            Move-Item -LiteralPath $backup -Destination $Target
            $oldExe = Join-Path $Target $ExecutableName
            Start-Process -FilePath $oldExe -WorkingDirectory $Target
            Write-UpdateLog "Previous version restored and restarted"
        }
    }
    catch {
        Write-UpdateLog "Rollback failed: $($_.Exception.Message)"
    }
    exit 1
}
""".strip()
