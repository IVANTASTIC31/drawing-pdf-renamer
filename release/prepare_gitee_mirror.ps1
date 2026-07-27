[CmdletBinding()]
param(
    [string]$Version = "0.1.6",
    [ValidateRange(1, 99)]
    [int]$PartSizeMiB = 90
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReleaseRoot = Join-Path $ProjectRoot "dist\release"
$MirrorRoot = Join-Path $ReleaseRoot "gitee"
$ArchiveName = "DrawingPdfRenamer-v$Version-windows-portable.zip"
$ArchivePath = Join-Path $ReleaseRoot $ArchiveName
$ManifestPath = Join-Path $PSScriptRoot "update-manifest.json"
$PartSize = $PartSizeMiB * 1MB
$GiteeDownloadRoot = "https://gitee.com/IVANTASTIC31/drawing-pdf-renamer/releases/download/v$Version"
$GitHubDownloadRoot = "https://github.com/IVANTASTIC31/drawing-pdf-renamer/releases/download/v$Version"

function Assert-SafeBuildPath {
    param([string]$Path)
    $resolvedProject = [IO.Path]::GetFullPath($ProjectRoot)
    $resolvedTarget = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedTarget.StartsWith($resolvedProject + [IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to modify a path outside the project: $resolvedTarget"
    }
}

if ($Version -notmatch "^\d+\.\d+\.\d+$") {
    throw "Version must use the 1.2.3 format."
}
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Portable archive not found: $ArchivePath"
}

Assert-SafeBuildPath $MirrorRoot
if (Test-Path -LiteralPath $MirrorRoot) {
    Remove-Item -LiteralPath $MirrorRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $MirrorRoot -Force | Out-Null

$archiveInfo = Get-Item -LiteralPath $ArchivePath
$archiveHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$buffer = New-Object byte[] (4MB)
$parts = [System.Collections.Generic.List[object]]::new()
$input = [IO.File]::OpenRead($ArchivePath)
try {
    $index = 1
    while ($input.Position -lt $input.Length) {
        $partName = "$ArchiveName.$($index.ToString('000'))"
        $partPath = Join-Path $MirrorRoot $partName
        $output = [IO.File]::Create($partPath)
        try {
            $written = 0L
            while ($written -lt $PartSize -and $input.Position -lt $input.Length) {
                $remaining = [Math]::Min($buffer.Length, $PartSize - $written)
                $read = $input.Read($buffer, 0, [int]$remaining)
                if ($read -le 0) {
                    break
                }
                $output.Write($buffer, 0, $read)
                $written += $read
            }
        }
        finally {
            $output.Dispose()
        }
        $partInfo = Get-Item -LiteralPath $partPath
        if ($partInfo.Length -gt 100MB) {
            throw "Gitee attachment exceeds 100 MiB: $partName"
        }
        $parts.Add([ordered]@{
            name = $partName
            url = "$GiteeDownloadRoot/$partName"
            size = $partInfo.Length
            sha256 = (Get-FileHash -LiteralPath $partPath -Algorithm SHA256).Hash.ToLowerInvariant()
        })
        $index += 1
    }
}
finally {
    $input.Dispose()
}

$releaseNotesPath = Join-Path $PSScriptRoot "RELEASE_NOTES.md"
$releaseNotes = if (Test-Path -LiteralPath $releaseNotesPath) {
    Get-Content -LiteralPath $releaseNotesPath -Raw -Encoding UTF8
}
else {
    "工程图纸 PDF 半自动重命名工具 v$Version"
}

$manifest = [ordered]@{
    version = $Version
    tag_name = "v$Version"
    notes = $releaseNotes.Trim()
    release_url = "https://gitee.com/IVANTASTIC31/drawing-pdf-renamer/releases/tag/v$Version"
    published_at = [DateTimeOffset]::Now.ToString("o")
    asset = [ordered]@{
        name = $ArchiveName
        size = $archiveInfo.Length
        sha256 = $archiveHash
        fallback_url = "$GitHubDownloadRoot/$ArchiveName"
        parts = $parts
    }
}

$manifestJson = $manifest | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText(
    $ManifestPath,
    $manifestJson,
    [Text.UTF8Encoding]::new($false)
)
Copy-Item -LiteralPath $ManifestPath -Destination (Join-Path $MirrorRoot "update-manifest.json")

$checksumLines = foreach ($part in $parts) {
    "$($part.sha256)  $($part.name)"
}
[IO.File]::WriteAllLines(
    (Join-Path $MirrorRoot "CHECKSUMS-SHA256.txt"),
    $checksumLines,
    [Text.UTF8Encoding]::new($false)
)

Write-Host ""
Write-Host "Gitee mirror files prepared:" -ForegroundColor Green
Get-ChildItem -LiteralPath $MirrorRoot -File |
    Select-Object Name, @{Name="MiB"; Expression={[Math]::Round($_.Length / 1MB, 2)}} |
    Format-Table -AutoSize
Write-Host "Manifest: $ManifestPath"
