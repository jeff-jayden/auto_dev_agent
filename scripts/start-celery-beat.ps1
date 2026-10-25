$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentFile = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw "Missing local configuration file: $environmentFile"
}

foreach ($rawLine in Get-Content -LiteralPath $environmentFile) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }
    $parts = $line.Split("=", 2)
    if ($parts.Count -ne 2 -or -not $parts[0].Trim()) {
        throw "Invalid .env entry (expected NAME=VALUE)"
    }
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1], "Process")
}

$env:PYTHONPATH = "src"
$env:EXECUTION_BACKEND = "celery"
Set-Location -LiteralPath $projectRoot
$scheduleDirectory = Join-Path $projectRoot "runtime\celery"
New-Item -ItemType Directory -Path $scheduleDirectory -Force | Out-Null
$scheduleFile = Join-Path $scheduleDirectory "celerybeat-schedule"
python -m celery -A execution.celery_app:celery_app beat --loglevel=INFO --schedule $scheduleFile
