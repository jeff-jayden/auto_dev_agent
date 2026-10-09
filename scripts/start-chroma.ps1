# 启动单个本地向量服务，API / Celery Worker 统一经 HTTP 访问，不能并发直接写目录。
param(
    [int]$Port = 8001,
    [string]$DataDirectory = ""
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DataDirectory) { $DataDirectory = Join-Path $projectRoot "runtime\vectors" }
Set-Location -LiteralPath $projectRoot
chroma run --host 127.0.0.1 --port $Port --path $DataDirectory
