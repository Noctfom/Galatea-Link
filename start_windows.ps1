# Galatea Link Windows 本地一键自检启动脚本，准备 ONNX 环境并启动服务

[CmdletBinding()]
param(
    [ValidateSet("requirements-onnx.txt", "requirements-pytorch.txt", "requirements.txt")]
    [string]$Requirements = "requirements-onnx.txt",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$localProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $localProjectRoot

$localVenvRoot = Join-Path $localProjectRoot ".venv"
$localVenvPythonCandidates = @(
    (Join-Path $localVenvRoot "Scripts\python.exe"),
    (Join-Path $localVenvRoot "bin\python.exe")
)
$localPythonCandidates = @($localVenvPythonCandidates)
if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
        $localPythonOutput = & py -3.11 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $localPythonCandidates += ($localPythonOutput | Select-Object -Last 1).Trim()
        }
    }
    catch {
        $localPythonOutput = $null
    }
}
$localPythonCandidates += @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:ProgramFiles "Python311\python.exe"),
    (Join-Path $env:ProgramFiles "Python312\python.exe")
)
foreach ($localPythonCommand in @("python3.11", "python3", "python")) {
    $localResolvedCommand = Get-Command $localPythonCommand -ErrorAction SilentlyContinue
    if ($localResolvedCommand) {
        $localPythonCandidates += $localResolvedCommand.Source
    }
}

$localBootstrapPython = ""
foreach ($localPythonCandidate in ($localPythonCandidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $localPythonCandidate -PathType Leaf -ErrorAction SilentlyContinue)) {
        continue
    }
    try {
        & $localPythonCandidate -c "import sys, sysconfig; platform=sysconfig.get_platform().lower(); raise SystemExit(0 if sys.version_info >= (3, 11) and not platform.startswith(('mingw', 'msys')) else 1)" 2>$null
        $localCandidateExitCode = $LASTEXITCODE
    }
    catch {
        $localCandidateExitCode = 1
    }
    if ($localCandidateExitCode -eq 0) {
        $localBootstrapPython = $localPythonCandidate
        break
    }
}
if (-not $localBootstrapPython) {
    throw "未找到可用的标准 CPython，请安装 Python 3.11 或更高版本，MSYS 与 MinGW Python 不受支持"
}

$localVenvPython = $localVenvPythonCandidates | Where-Object {
    Test-Path -LiteralPath $_ -PathType Leaf -ErrorAction SilentlyContinue
} | Select-Object -First 1
if (-not $localVenvPython) {
    Write-Host "正在创建 Galatea Link Python 隔离环境"
    & $localBootstrapPython -m venv $localVenvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 隔离环境失败"
    }
    $localVenvPython = $localVenvPythonCandidates | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf -ErrorAction SilentlyContinue
    } | Select-Object -First 1
}
if (-not $localVenvPython) {
    throw "隔离环境中没有找到 Python 可执行文件"
}

$localRequirementFiles = Get-ChildItem -LiteralPath $localProjectRoot -Filter "requirements*.txt" -File | Sort-Object Name
$localRequirementFingerprint = $Requirements + "|" + (($localRequirementFiles | ForEach-Object {
    (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
}) -join "|")
$localRequirementStamp = Join-Path $localVenvRoot ".galatea-requirements.sha256"
$localInstalledFingerprint = if (Test-Path -LiteralPath $localRequirementStamp -PathType Leaf) {
    [IO.File]::ReadAllText($localRequirementStamp).Trim()
}
else {
    ""
}

if ($localInstalledFingerprint -ne $localRequirementFingerprint) {
    Write-Host "正在安装或更新 $Requirements"
    & $localVenvPython -m pip install --disable-pip-version-check -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Python 依赖安装失败"
    }
    [IO.File]::WriteAllText(
        $localRequirementStamp,
        $localRequirementFingerprint,
        (New-Object Text.UTF8Encoding($false))
    )
}
else {
    Write-Host "Python 依赖自检通过"
}

$localConfigPath = Join-Path $localProjectRoot "config.yaml"
if (-not (Test-Path -LiteralPath $localConfigPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $localProjectRoot "config.example.yaml") -Destination $localConfigPath
    Write-Host "已生成本机 config.yaml"
}

foreach ($localRuntimeDirectory in @("decks", "models", "model_assets\v3", "deploy_packages")) {
    New-Item -ItemType Directory -Path (Join-Path $localProjectRoot $localRuntimeDirectory) -Force | Out-Null
}

$localBackendModule = if ($Requirements -eq "requirements-pytorch.txt") { "torch" } else { "onnxruntime" }
$localWebAddress = & $localVenvPython -c "import aiohttp, httpx, numpy, yaml, $localBackendModule; from app_config import load_app_config; config=load_app_config('config.yaml'); host='127.0.0.1' if config.service.host in ('0.0.0.0', '::') else config.service.host; print(f'http://{host}:{config.service.port}')"
if ($LASTEXITCODE -ne 0) {
    throw "依赖或 config.yaml 自检失败"
}
$localWebAddress = ($localWebAddress | Select-Object -Last 1).Trim()

if ($CheckOnly) {
    Write-Host "Galatea Link 本地环境自检完成"
    exit 0
}

Write-Host "正在启动 Galatea Link：$localWebAddress"
& $localVenvPython scripts/run_link_service.py --config config.yaml
exit $LASTEXITCODE
