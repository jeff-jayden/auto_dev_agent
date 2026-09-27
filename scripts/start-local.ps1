$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentFile = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw "Missing local configuration file: $environmentFile"
}

foreach ($rawLine in Get-Content -LiteralPath $environmentFile) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#")) {
        continue
    }
    $parts = $line.Split("=", 2)
    if ($parts.Count -ne 2 -or -not $parts[0].Trim()) {
        throw "Invalid .env entry (expected NAME=VALUE)"
    }
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1], "Process")
}

$env:PYTHONPATH = "src"
Set-Location -LiteralPath $projectRoot
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8765
