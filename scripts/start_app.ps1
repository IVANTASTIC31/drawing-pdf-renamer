[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$NoPause,
    [switch]$ConsoleMode
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$VenvPythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$MainScript = Join-Path $ProjectRoot "main.py"
$LogDirectory = Join-Path $env:LOCALAPPDATA "DrawingPdfRenamer\logs"

function Wait-ForUser {
    if (-not $NoPause) {
        [void](Read-Host "按 Enter 键关闭窗口")
    }
}

function Stop-WithMessage {
    param([string]$Message)
    Write-Host ""
    Write-Host "[启动失败] $Message" -ForegroundColor Red
    Write-Host "程序日志目录：$LogDirectory" -ForegroundColor Yellow
    Write-Host "可运行“启动程序.bat --console”查看控制台错误。" -ForegroundColor Yellow
    Wait-ForUser
    exit 1
}

try {
    Set-Location -LiteralPath $ProjectRoot
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Stop-WithMessage "尚未安装依赖。请先双击“安装依赖.bat”，等待安装完成后再启动。"
    }
    if (-not (Test-Path -LiteralPath $MainScript -PathType Leaf)) {
        Stop-WithMessage "找不到 main.py。请重新复制完整项目文件夹，不要单独移动启动脚本。"
    }

    $env:PYTHONPATH = Join-Path $ProjectRoot "src"
    Write-Host "正在检查运行环境..."
    & $VenvPython -c "from drawing_renamer.app import main; print('运行环境检查通过')"
    if ($LASTEXITCODE -ne 0) {
        Stop-WithMessage "Python环境缺少组件或已经损坏，请重新运行“安装依赖.bat”修复。"
    }

    if ($CheckOnly) {
        Write-Host "检查完成，程序可以正常启动。" -ForegroundColor Green
        Wait-ForUser
        exit 0
    }

    if ($ConsoleMode) {
        Write-Host "正在以诊断模式启动；关闭程序后，本窗口会保留错误信息。"
        & $VenvPython $MainScript
        if ($LASTEXITCODE -ne 0) {
            Stop-WithMessage "程序异常退出，退出代码：$LASTEXITCODE"
        }
        exit 0
    }

    if (-not (Test-Path -LiteralPath $VenvPythonw -PathType Leaf)) {
        Stop-WithMessage "找不到 pythonw.exe，虚拟环境不完整，请重新运行安装脚本。"
    }

    $QuotedMainScript = '"' + $MainScript + '"'
    Start-Process -FilePath $VenvPythonw -ArgumentList $QuotedMainScript -WorkingDirectory $ProjectRoot
    exit 0
}
catch {
    Stop-WithMessage $_.Exception.Message
}
