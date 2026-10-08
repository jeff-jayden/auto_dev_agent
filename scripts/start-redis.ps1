$ErrorActionPreference = "Stop"

$existing = (wsl.exe docker ps -a --filter "name=^/ai-dev-agent-redis$" --format "{{.Names}}")
if ($existing -contains "ai-dev-agent-redis") {
    wsl.exe docker start ai-dev-agent-redis | Out-Null
} else {
    wsl.exe docker volume create ai-dev-agent-redis-data | Out-Null
    wsl.exe docker run -d `
        --name ai-dev-agent-redis `
        --restart unless-stopped `
        -p 127.0.0.1:6379:6379 `
        -v ai-dev-agent-redis-data:/data `
        redis:7-alpine redis-server --appendonly yes | Out-Null
}
if ($LASTEXITCODE -ne 0) { throw "docker run failed to start Redis" }
wsl.exe docker ps --filter "name=^/ai-dev-agent-redis$" `
    --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
