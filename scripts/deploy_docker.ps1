# Galatea Link Windows Docker 快速部署脚本，初始化访问令牌并启动服务

$ErrorActionPreference = "Stop"

$deployScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$deployProjectRoot = Split-Path -Parent $deployScriptRoot
Set-Location -LiteralPath $deployProjectRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 Docker，请先安装并启动 Docker Desktop"
}

& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "当前 Docker 未提供 Compose v2"
}

$deployEnvPath = Join-Path $deployProjectRoot ".env"
if (-not (Test-Path -LiteralPath $deployEnvPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $deployProjectRoot ".env.example") -Destination $deployEnvPath
}

$deployEnvText = [IO.File]::ReadAllText($deployEnvPath)
$deployTokenMatch = [Text.RegularExpressions.Regex]::Match(
    $deployEnvText,
    "(?m)^GALATEA_LINK_API_TOKEN=(.*)$"
)
if (-not $deployTokenMatch.Success -or [string]::IsNullOrWhiteSpace($deployTokenMatch.Groups[1].Value)) {
    $deployTokenBytes = New-Object byte[] 32
    $deployRandom = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $deployRandom.GetBytes($deployTokenBytes)
    }
    finally {
        $deployRandom.Dispose()
    }
    $deployToken = ([BitConverter]::ToString($deployTokenBytes) -replace "-", "").ToLowerInvariant()
    if ($deployTokenMatch.Success) {
        $deployEnvText = [Text.RegularExpressions.Regex]::Replace(
            $deployEnvText,
            "(?m)^GALATEA_LINK_API_TOKEN=.*$",
            "GALATEA_LINK_API_TOKEN=$deployToken"
        )
    }
    else {
        $deployEnvText = $deployEnvText.TrimEnd() + "`nGALATEA_LINK_API_TOKEN=$deployToken`n"
    }
    [IO.File]::WriteAllText(
        $deployEnvPath,
        $deployEnvText,
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Host "已在 .env 中生成随机 Link API Token"
}

& docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Galatea Link Docker 服务启动失败"
}

$deployPortMatch = [Text.RegularExpressions.Regex]::Match(
    [IO.File]::ReadAllText($deployEnvPath),
    "(?m)^GALATEA_LINK_PORT=(\d+)$"
)
$deployPort = if ($deployPortMatch.Success) { $deployPortMatch.Groups[1].Value } else { "8765" }
Write-Host "Galatea Link 已启动：http://127.0.0.1:$deployPort"
Write-Host "查看日志：docker compose logs -f galatea-link"
