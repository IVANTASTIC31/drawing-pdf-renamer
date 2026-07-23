[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvDirectory = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDirectory "Scripts\python.exe"
$Requirements = Join-Path $ProjectRoot "requirements.txt"
$LogDirectory = Join-Path $ProjectRoot "安装日志"
$LogFile = Join-Path $LogDirectory "pip-install.log"

function Wait-ForUser {
    if (-not $NoPause) {
        [void](Read-Host "按 Enter 键关闭窗口")
    }
}

function Stop-WithMessage {
    param([string]$Message)
    Write-Host ""
    Write-Host "[错误] $Message" -ForegroundColor Red
    Write-Host "安装未完成，程序尚不能正常启动。" -ForegroundColor Yellow
    Wait-ForUser
    exit 1
}

function Test-CompatiblePython {
    param(
        [string]$Executable,
        [string[]]$PrefixArguments
    )
    try {
        & $Executable @PrefixArguments -c "import struct,sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,13) and struct.calcsize('P') == 8 else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Find-CompatiblePython {
    $Candidates = @(
        @{ Executable = "py"; Prefix = @("-3.12") },
        @{ Executable = "py"; Prefix = @("-3.11") },
        @{ Executable = "python"; Prefix = @() }
    )
    foreach ($Candidate in $Candidates) {
        if (-not (Get-Command $Candidate.Executable -ErrorAction SilentlyContinue)) {
            continue
        }
        if (Test-CompatiblePython -Executable $Candidate.Executable -PrefixArguments $Candidate.Prefix) {
            return $Candidate
        }
    }
    return $null
}

function Invoke-PipInstall {
    param(
        [string]$SourceName,
        [string]$IndexUrl
    )
    Write-Host "当前下载源：$SourceName" -ForegroundColor Cyan
    & $VenvPython -m pip install -r $Requirements --prefer-binary --timeout 90 --retries 3 -i $IndexUrl --log $LogFile
    return $LASTEXITCODE -eq 0
}

try {
    Set-Location -LiteralPath $ProjectRoot
    Write-Host "============================================================"
    Write-Host " 工程图纸 PDF 半自动重命名工具 - 依赖安装"
    Write-Host "============================================================"
    Write-Host "项目目录：$ProjectRoot"
    Write-Host ""

    if ($CheckOnly) {
        if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
            Stop-WithMessage "尚未安装虚拟环境，请直接双击“安装依赖.bat”完成安装。"
        }
    }
    elseif (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        if (Test-Path -LiteralPath $VenvDirectory) {
            Stop-WithMessage "发现损坏或不完整的 .venv 文件夹。请关闭程序，将该文件夹删除或改名后重试：$VenvDirectory"
        }

        Write-Host "[1/4] 正在查找兼容的 Python 64 位版本..."
        $Python = Find-CompatiblePython
        if ($null -eq $Python) {
            Stop-WithMessage "未找到 Python 3.12/3.11 64 位版本。请安装并勾选 Add Python to PATH：https://www.python.org/downloads/windows/"
        }

        $PythonExecutable = [string]$Python.Executable
        [string[]]$PythonPrefixArguments = @($Python.Prefix)
        & $PythonExecutable @PythonPrefixArguments --version
        Write-Host "[2/4] 正在创建独立虚拟环境 .venv，请稍候..."
        & $PythonExecutable @PythonPrefixArguments -m venv $VenvDirectory
        if ($LASTEXITCODE -ne 0) {
            Stop-WithMessage "创建虚拟环境失败。请确认项目目录可写、磁盘空间充足，并避免放在受限的系统目录中。"
        }
    }
    elseif (-not $CheckOnly) {
        Write-Host "[1/4] 已找到现有虚拟环境，将检查并补齐依赖。"
    }

    if (-not $CheckOnly) {
        New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
        if (Test-Path -LiteralPath $LogFile) {
            Remove-Item -LiteralPath $LogFile -Force
        }

        Write-Host "[2/4] 正在准备 pip 安装工具..."
        & $VenvPython -m pip install --upgrade pip setuptools wheel --prefer-binary --timeout 60 --retries 3 -i "https://pypi.tuna.tsinghua.edu.cn/simple" --log $LogFile
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[提示] 国内镜像升级 pip 失败，将使用当前 pip 继续。" -ForegroundColor Yellow
        }

        Write-Host "[3/4] 正在安装程序依赖，首次安装文件较大，请耐心等待..."
        $Sources = @(
            @{ Name = "清华大学镜像"; Url = "https://pypi.tuna.tsinghua.edu.cn/simple" },
            @{ Name = "阿里云镜像"; Url = "https://mirrors.aliyun.com/pypi/simple" },
            @{ Name = "PyPI 官方源"; Url = "https://pypi.org/simple" }
        )
        $Installed = $false
        foreach ($Source in $Sources) {
            if (Invoke-PipInstall -SourceName $Source.Name -IndexUrl $Source.Url) {
                $Installed = $true
                break
            }
            Write-Host "[提示] 当前下载源失败，准备切换下一个源重试。" -ForegroundColor Yellow
        }
        if (-not $Installed) {
            Stop-WithMessage "三个下载源均失败。可能是网络代理、杀毒软件、Python版本或磁盘空间问题。请提供日志：$LogFile"
        }
    }

    Write-Host "[4/4] 正在检查依赖完整性..."
    & $VenvPython -c "import importlib.util; names=('PySide6','fitz','PIL','numpy','paddleocr','paddle'); missing=[n for n in names if importlib.util.find_spec(n) is None]; print('缺少模块：'+', '.join(missing)) if missing else print('核心依赖检查通过'); raise SystemExit(1 if missing else 0)"
    if ($LASTEXITCODE -ne 0) {
        Stop-WithMessage "依赖完整性检查未通过。请重新运行安装脚本；若仍失败，请提供日志：$LogFile"
    }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        Stop-WithMessage "pip 检测到依赖版本冲突。请重新运行安装脚本；若仍失败，请提供日志：$LogFile"
    }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " 安装和检查完成！现在可以双击“启动程序.bat”。" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    if (Test-Path -LiteralPath $LogFile) {
        Write-Host "安装日志：$LogFile"
    }
    Wait-ForUser
    exit 0
}
catch {
    Stop-WithMessage $_.Exception.Message
}
