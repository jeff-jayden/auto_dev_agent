$ErrorActionPreference = "Stop"

if (-not $env:MODEL_PROVIDER) { $env:MODEL_PROVIDER = "ollama" }
if (-not $env:MODEL_NAME) { $env:MODEL_NAME = "qwen3:4b" }
if (-not $env:MODEL_BASE_URL) { $env:MODEL_BASE_URL = "http://127.0.0.1:11434/v1" }
if (-not $env:MODEL_API_KEY) { $env:MODEL_API_KEY = "ollama" }
if (-not $env:GITHUB_TOKEN) {
    $env:GITHUB_TOKEN = Read-Host -MaskInput "GitHub token (leave empty to disable GitHub delivery)"
}
$env:PYTHONPATH = "src"

python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8765
