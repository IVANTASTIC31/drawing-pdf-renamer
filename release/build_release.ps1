[CmdletBinding()]
param(
    [string]$Version = "0.1.5"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$OutputRoot = Join-Path $ProjectRoot "dist\release"
$WorkRoot = Join-Path $ProjectRoot "build\release"
$PortableName = "DrawingPdfRenamer-v$Version-windows-portable"
$PortableBuild = Join-Path $WorkRoot "pyinstaller-dist"
$PortableStage = Join-Path $PortableBuild "DrawingPdfRenamer"

function Assert-SafeBuildPath {
    param([string]$Path)
    $resolvedProject = [IO.Path]::GetFullPath($ProjectRoot)
    $resolvedTarget = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedTarget.StartsWith($resolvedProject + [IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to modify a path outside the project: $resolvedTarget"
    }
}

function New-ZipArchive {
    param([string]$SourceDirectory, [string]$DestinationFile)
    if (Test-Path -LiteralPath $DestinationFile) {
        Remove-Item -LiteralPath $DestinationFile -Force
    }
    Compress-Archive -Path (Join-Path $SourceDirectory "*") -DestinationPath $DestinationFile -CompressionLevel Optimal
}

if ($Version -notmatch "^\d+\.\d+\.\d+$") {
    throw "Version must use the 1.2.3 format."
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project virtual environment not found. Run the dependency installer first."
}

Assert-SafeBuildPath $OutputRoot
Assert-SafeBuildPath $WorkRoot
if (Test-Path -LiteralPath $OutputRoot) {
    Remove-Item -LiteralPath $OutputRoot -Recurse -Force
}
if (Test-Path -LiteralPath $WorkRoot) {
    Remove-Item -LiteralPath $WorkRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

& $Python -m PyInstaller --noconfirm --clean `
    --distpath $PortableBuild `
    --workpath (Join-Path $WorkRoot "pyinstaller-work") `
    (Join-Path $PSScriptRoot "DrawingPdfRenamer.spec")
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $PortableStage -PathType Container)) {
    throw "PyInstaller portable build failed."
}

$SmokeRoot = Join-Path $WorkRoot "portable-smoke"
New-Item -ItemType Directory -Path $SmokeRoot -Force | Out-Null
$SmokeInput = Join-Path $SmokeRoot "input.png"
$SmokeOutput = Join-Path $SmokeRoot "result.json"
$SmokeCrash = Join-Path $SmokeRoot "crash.log"
$SmokeProgress = Join-Path $SmokeRoot "progress.json"
& $Python -c "import sys; from PIL import Image, ImageDraw; image=Image.new('RGB',(500,100),'white'); ImageDraw.Draw(image).text((20,30),'CP41.100A',fill='black'); image.save(sys.argv[1])" $SmokeInput
if ($LASTEXITCODE -ne 0) {
    throw "Could not create the portable OCR smoke-test image."
}
$PortableExe = Join-Path $PortableStage "DrawingPdfRenamer.exe"
$SmokeArguments = @(
    "--ocr-worker",
    "recognize",
    "`"$SmokeInput`"",
    "`"$SmokeOutput`"",
    "`"$SmokeCrash`"",
    "`"$SmokeProgress`""
)
$SmokeProcess = Start-Process -FilePath $PortableExe -ArgumentList $SmokeArguments -WorkingDirectory $SmokeRoot -WindowStyle Hidden -Wait -PassThru
if ($SmokeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $SmokeOutput -PathType Leaf)) {
    throw "Portable OCR worker did not start successfully. See: $SmokeCrash"
}
$SmokeResult = Get-Content -LiteralPath $SmokeOutput -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $SmokeResult.ok) {
    throw "Portable OCR smoke test failed: $($SmokeResult.error). See: $SmokeCrash"
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "portable-readme.txt") -Destination (Join-Path $PortableStage "README-FIRST.txt")
$forbiddenPortable = Get-ChildItem -LiteralPath $PortableStage -File -Recurse -Force |
    Where-Object {
        $_.Extension -in @(".pdf", ".log") -or
        $_.FullName -match "(?i)[\\/](history|logs)[\\/]"
    }
if ($forbiddenPortable) {
    throw "Portable package contains forbidden user files: $($forbiddenPortable.FullName -join ', ')"
}
$PortableZip = Join-Path $OutputRoot "$PortableName.zip"
New-ZipArchive -SourceDirectory $PortableStage -DestinationFile $PortableZip

$artifacts = Get-ChildItem -LiteralPath $OutputRoot -Filter "*.zip" -File
$checksumFile = Join-Path $OutputRoot "CHECKSUMS-SHA256.txt"
$checksumLines = foreach ($artifact in $artifacts) {
    $hash = Get-FileHash -LiteralPath $artifact.FullName -Algorithm SHA256
    "$($hash.Hash.ToLowerInvariant())  $($artifact.Name)"
}
[IO.File]::WriteAllLines($checksumFile, $checksumLines, [Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "Release packages built:" -ForegroundColor Green
Get-ChildItem -LiteralPath $OutputRoot -File |
    Select-Object Name, @{Name="MiB"; Expression={[math]::Round($_.Length / 1MB, 2)}} |
    Format-Table -AutoSize
