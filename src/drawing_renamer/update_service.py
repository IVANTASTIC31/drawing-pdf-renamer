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
from typing import Any, Callable

import certifi

from .subprocess_visibility import hidden_window_options


logger = logging.getLogger("drawing_renamer.update")

REPOSITORY = "IVANTASTIC31/drawing-pdf-renamer"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
GITEE_REPOSITORY = "IVANTASTIC31/drawing-pdf-renamer"
GITEE_UPDATE_MANIFEST = (
    f"https://gitee.com/{GITEE_REPOSITORY}/raw/main/release/update-manifest.json"
)
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
class ReleasePart:
    name: str
    download_url: str
    size: int
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int
    sha256: str
    parts: tuple[ReleasePart, ...] = ()
    source: str = "GitHub"


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


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    downloaded: int
    total: int
    source: str
    stage: str = "download"


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
    def __init__(
        self,
        repository: str = REPOSITORY,
        gitee_manifest_url: str | None = None,
    ) -> None:
        self.repository = repository
        self.latest_release_api = f"https://api.github.com/repos/{repository}/releases/latest"
        if gitee_manifest_url is None and repository == REPOSITORY:
            gitee_manifest_url = GITEE_UPDATE_MANIFEST
        self.gitee_manifest_url = gitee_manifest_url or ""

    def check(self, current_version: str, edition: str | None = None) -> UpdateInfo | None:
        edition = edition or distribution_edition()
        logger.info(
            "Checking for updates: repository=%s current=%s edition=%s",
            self.repository,
            current_version,
            edition,
        )
        if self.gitee_manifest_url:
            try:
                gitee_info = self._check_gitee(current_version)
            except UpdateError as exc:
                logger.warning("Gitee mirror check failed; falling back to GitHub: %s", exc)
            else:
                if gitee_info is not None:
                    return gitee_info
        return self._check_github(current_version, edition)

    def _check_gitee(self, current_version: str) -> UpdateInfo | None:
        try:
            payload = json.loads(_request_bytes(self.gitee_manifest_url).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpdateError("Gitee 更新清单无法解析") from exc
        tag_name = str(payload.get("tag_name") or payload.get("version") or "")
        try:
            latest_version_tuple = parse_version(tag_name)
            current_version_tuple = parse_version(current_version)
        except ValueError as exc:
            raise UpdateError(f"Gitee 更新清单版本号无效：{exc}") from exc
        if latest_version_tuple <= current_version_tuple:
            return None

        version = ".".join(str(part) for part in latest_version_tuple)
        expected_name = f"DrawingPdfRenamer-v{version}-windows-portable.zip"
        asset_payload = payload.get("asset")
        if not isinstance(asset_payload, dict):
            raise UpdateError("Gitee 更新清单缺少免安装包信息")
        asset_name = str(asset_payload.get("name") or "")
        if asset_name != expected_name:
            raise UpdateError(f"Gitee 更新清单中的文件名不匹配：{asset_name}")
        sha256 = str(asset_payload.get("sha256") or "").lower()
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise UpdateError("Gitee 更新清单缺少有效的 SHA256")
        try:
            asset_size = int(asset_payload.get("size") or 0)
        except (TypeError, ValueError) as exc:
            raise UpdateError("Gitee 更新清单中的文件大小无效") from exc
        if asset_size <= 0:
            raise UpdateError("Gitee 更新清单中的文件大小无效")

        raw_parts = asset_payload.get("parts")
        if not isinstance(raw_parts, list) or not raw_parts:
            raise UpdateError("Gitee 更新清单缺少下载分卷")
        parts: list[ReleasePart] = []
        for raw_part in raw_parts:
            if not isinstance(raw_part, dict):
                raise UpdateError("Gitee 更新清单中的下载分卷无效")
            name = str(raw_part.get("name") or "")
            url = str(raw_part.get("url") or "")
            part_sha256 = str(raw_part.get("sha256") or "").lower()
            try:
                size = int(raw_part.get("size") or 0)
            except (TypeError, ValueError) as exc:
                raise UpdateError(f"Gitee 下载分卷大小无效：{name}") from exc
            if not name or not url.startswith("https://") or size <= 0:
                raise UpdateError(f"Gitee 下载分卷信息不完整：{name or '未命名分卷'}")
            if len(part_sha256) != 64 or any(
                char not in "0123456789abcdef" for char in part_sha256
            ):
                raise UpdateError(f"Gitee 下载分卷缺少有效的 SHA256：{name}")
            parts.append(ReleasePart(name, url, size, part_sha256))
        if sum(part.size for part in parts) != asset_size:
            raise UpdateError("Gitee 下载分卷总大小与安装包不一致")

        fallback_url = str(asset_payload.get("fallback_url") or "")
        if fallback_url and not fallback_url.startswith("https://"):
            raise UpdateError("GitHub 备用下载地址无效")
        return UpdateInfo(
            version=version,
            tag_name=str(payload.get("tag_name") or f"v{version}"),
            notes=str(payload.get("notes") or "本次发布未填写更新说明。"),
            release_url=str(payload.get("release_url") or ""),
            published_at=str(payload.get("published_at") or ""),
            asset=ReleaseAsset(
                name=asset_name,
                download_url=fallback_url,
                size=asset_size,
                sha256=sha256,
                parts=tuple(parts),
                source="Gitee 国内镜像",
            ),
        )

    def _check_github(self, current_version: str, edition: str) -> UpdateInfo | None:
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
                source="GitHub",
            ),
        )

    def download_and_prepare(
        self,
        info: UpdateInfo,
        root: Path,
        progress: Callable[[DownloadProgress], None] | None = None,
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

        if info.asset.parts:
            sources = info.asset.parts
            source_label = info.asset.source
        else:
            sources = (
                ReleasePart(info.asset.name, info.asset.download_url, info.asset.size),
            )
            source_label = info.asset.source
        active_source_label = source_label
        try:
            downloaded, digest = self._download_sources(
                sources,
                partial_path,
                info.asset.size,
                source_label,
                progress,
                cancelled,
            )
        except UpdateCancelledError:
            partial_path.unlink(missing_ok=True)
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, UpdateError) as exc:
            if info.asset.parts and info.asset.download_url:
                logger.warning(
                    "Gitee mirror download failed; retrying from GitHub: %s",
                    exc,
                )
                partial_path.unlink(missing_ok=True)
                try:
                    active_source_label = "GitHub 备用源"
                    downloaded, digest = self._download_sources(
                        (
                            ReleasePart(
                                info.asset.name,
                                info.asset.download_url,
                                info.asset.size,
                            ),
                        ),
                        partial_path,
                        info.asset.size,
                        active_source_label,
                        progress,
                        cancelled,
                    )
                except UpdateCancelledError:
                    partial_path.unlink(missing_ok=True)
                    raise
                except (
                    OSError,
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                    TimeoutError,
                    UpdateError,
                ) as fallback_exc:
                    partial_path.unlink(missing_ok=True)
                    raise UpdateError(f"Gitee 与 GitHub 下载均失败：{fallback_exc}") from fallback_exc
            else:
                partial_path.unlink(missing_ok=True)
                raise UpdateError(f"下载安装包失败：{exc}") from exc

        if progress:
            progress(
                DownloadProgress(
                    downloaded,
                    info.asset.size,
                    active_source_label,
                    "verify",
                )
            )
        actual_digest = digest.hexdigest().lower()
        if actual_digest != info.asset.sha256.lower():
            partial_path.unlink(missing_ok=True)
            raise UpdateError(
                "安装包 SHA256 校验失败，文件可能不完整或已被篡改，已停止更新"
            )
        partial_path.replace(archive_path)
        if progress:
            progress(
                DownloadProgress(
                    downloaded,
                    info.asset.size,
                    active_source_label,
                    "extract",
                )
            )
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
    def _download_sources(
        sources: tuple[ReleasePart, ...],
        partial_path: Path,
        total: int,
        source_label: str,
        progress: Callable[[DownloadProgress], None] | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[int, Any]:
        downloaded = 0
        digest = hashlib.sha256()
        if progress:
            progress(DownloadProgress(0, total, source_label))
        with partial_path.open("wb") as destination:
            for index, source in enumerate(sources, start=1):
                label = source_label
                if len(sources) > 1:
                    label = f"{source_label} · 分卷 {index}/{len(sources)}"
                request = urllib.request.Request(source.download_url, headers=REQUEST_HEADERS)
                part_downloaded = 0
                part_digest = hashlib.sha256()
                with _open_url(request, 30.0) as response:
                    while True:
                        if cancelled and cancelled():
                            raise UpdateCancelledError("已取消更新下载")
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        destination.write(chunk)
                        digest.update(chunk)
                        part_digest.update(chunk)
                        part_downloaded += len(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress(DownloadProgress(downloaded, total, label))
                if source.size and part_downloaded != source.size:
                    raise UpdateError(
                        f"下载分卷不完整：{source.name}，"
                        f"应为 {source.size} 字节，实际 {part_downloaded} 字节"
                    )
                if source.sha256 and part_digest.hexdigest().lower() != source.sha256.lower():
                    raise UpdateError(f"下载分卷校验失败：{source.name}")
        return downloaded, digest

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
