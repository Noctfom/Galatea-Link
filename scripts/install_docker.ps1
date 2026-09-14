# Galatea Link Windows 在线 Docker 安装脚本，下载仓库并调用快速部署入口

$ErrorActionPreference = "Stop"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "未找到 Git，请先安装 Git"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 Docker，请先安装并启动 Docker Desktop"
}

$dockerInstallCurrent = (Get-Location).Path
$dockerInstallConfigured = [Environment]::GetEnvironmentVariable("GALATEA_LINK_INSTALL_DIR")
if (
    (Test-Path -LiteralPath (Join-Path $dockerInstallCurrent "docker-compose.yml") -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $dockerInstallCurrent "scripts\deploy_docker.ps1") -PathType Leaf)
) {
    $dockerInstallProject = $dockerInstallCurrent
}
elseif ($dockerInstallConfigured) {
    $dockerInstallProject = [IO.Path]::GetFullPath($dockerInstallConfigured)
}
else {
    $dockerInstallProject = Join-Path $dockerInstallCurrent "Galatea-Link"
}

if (-not (Test-Path -LiteralPath $dockerInstallProject)) {
    & git clone --depth 1 https://github.com/Noctfom/Galatea-Link.git $dockerInstallProject
    if ($LASTEXITCODE -ne 0) {
        throw "Galatea Link 仓库下载失败"
    }
}
elseif (-not (Test-Path -LiteralPath (Join-Path $dockerInstallProject "scripts\deploy_docker.ps1") -PathType Leaf)) {
    throw "目标目录已经存在但不是 Galatea Link 仓库：$dockerInstallProject"
}

$dockerInstallPowerShell = (Get-Process -Id $PID).Path
& $dockerInstallPowerShell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dockerInstallProject "scripts\deploy_docker.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Galatea Link Docker 快速部署失败"
}
