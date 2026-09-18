param(
    [ValidateSet("neo4j", "sqlite")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"

function Test-LocalPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-PortOccupant([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    # No listener, or one we cannot attribute, both count as "not occupied".
    if (-not $listener -or -not $listener.OwningProcess) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
}

# A stale instance is left by a previous start.ps1: on Windows, Ctrl+C often only
# ends PowerShell itself, so the uvicorn child keeps holding port 8000 and the next
# run is rejected as a port conflict.
function Stop-StaleInstance($Process) {
    $commandLine = [string]$Process.CommandLine
    $looksLikeTraceGraph =
        $commandLine -like "*$projectRoot\.venv\Scripts\python.exe*" -and
        $commandLine -like "*uvicorn tracegraph.bootstrap:app*"
    if (-not $looksLikeTraceGraph) {
        throw "Port 8000 is held by an unrelated process (PID $($Process.ProcessId)). Stop it yourself and retry."
    }
    Write-Host "Port 8000 is held by a previous TraceGraph instance (PID $($Process.ProcessId)); stopping it." -ForegroundColor Yellow
    Stop-Process -Id $Process.ProcessId -Force -ErrorAction Stop
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (-not (Test-LocalPort 8000)) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "Port 8000 is still in use after stopping PID $($Process.ProcessId)"
}

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $projectRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match "^\s*([^#][^=]*)=(.*)$") {
            Set-Item -LiteralPath "Env:$($matches[1].Trim())" -Value $matches[2].Trim()
        }
    }
}
foreach ($name in @(
    "JAVA_HOME",
    "NEO4J_HOME",
    "TRACEGRAPH_GRAPH_BACKEND",
    "TRACEGRAPH_GRAPH_FALLBACK",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
    "TRACEGRAPH_GENERATOR",
    "TRACEGRAPH_LLM_BASE_URL",
    "TRACEGRAPH_LLM_API_KEY",
    "TRACEGRAPH_LLM_MODEL",
    "TRACEGRAPH_LLM_TIMEOUT",
    "TRACEGRAPH_LLM_FALLBACK"
)) {
    if (-not (Get-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue)) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($value) { Set-Item -LiteralPath "Env:$name" -Value $value }
    }
}

# -Mode overrides .env. SQLite is the dependency-free default.
if (-not $Mode) { $Mode = $env:TRACEGRAPH_GRAPH_BACKEND }
if (-not $Mode) { $Mode = "sqlite" }
$Mode = $Mode.ToLower()
$env:TRACEGRAPH_GRAPH_BACKEND = $Mode
$fallback = $env:TRACEGRAPH_GRAPH_FALLBACK
if (-not $fallback) { $fallback = "none" }

$blocked = $false
if ($Mode -eq "neo4j") {
    if (-not $env:JAVA_HOME -or -not (Test-Path -LiteralPath "$env:JAVA_HOME\bin\java.exe")) {
        Write-Host "JAVA_HOME does not point to the JDK on drive D." -ForegroundColor Red
        $blocked = $true
    }
    if (-not $env:NEO4J_HOME -or -not (Test-Path -LiteralPath "$env:NEO4J_HOME\bin\neo4j.bat")) {
        Write-Host "NEO4J_HOME does not point to Neo4j on drive D." -ForegroundColor Red
        $blocked = $true
    }
    if (-not $env:NEO4J_PASSWORD) {
        Write-Host "NEO4J_PASSWORD is not configured." -ForegroundColor Red
        $blocked = $true
    }

    if (-not $blocked -and -not (Test-LocalPort 7687)) {
        $stdout = Join-Path $env:NEO4J_HOME "logs\tracegraph-console.out.log"
        $stderr = Join-Path $env:NEO4J_HOME "logs\tracegraph-console.err.log"
        Start-Process `
            -FilePath "$env:NEO4J_HOME\bin\neo4j.bat" `
            -ArgumentList "console" `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr | Out-Null
        for ($attempt = 0; $attempt -lt 45; $attempt++) {
            Start-Sleep -Seconds 1
            if (Test-LocalPort 7687) {
                break
            }
        }
        if (-not (Test-LocalPort 7687)) {
            Write-Host "Neo4j did not accept connections on 7687 within 45s. See $stderr" -ForegroundColor Red
            $blocked = $true
        }
    }

    if ($blocked) {
        if ($fallback -eq "sqlite") {
            Write-Host "TRACEGRAPH_GRAPH_FALLBACK=sqlite: starting with the SQLite graph backend. /system will report the fallback." -ForegroundColor Yellow
        } else {
            Write-Host "Neo4j is unavailable. To use SQLite, run: .\start.ps1 -Mode sqlite" -ForegroundColor Yellow
            exit 1
        }
    }
}

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment was not found: $python"
}
if (Test-LocalPort 8000) {
    $occupant = Get-PortOccupant 8000
    # Reachable, but not necessarily attributable; never kill what we cannot identify.
    if (-not $occupant) {
        throw "Port 8000 is already in use, but its owner could not be identified."
    }
    Stop-StaleInstance $occupant
}

if ($Mode -eq "neo4j" -and -not $blocked) {
    Write-Host "Graph backend: Neo4j (http://localhost:7474)"
} elseif ($Mode -eq "neo4j") {
    Write-Host "Graph backend: SQLite (Neo4j request fell back; see /system)"
} else {
    Write-Host "Graph backend: SQLite ($projectRoot\data\local\tracegraph.db)"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "frontend\dist\index.html"))) {
    Write-Host "Frontend is not built; /app will return 503. Run: npm --prefix frontend install; npm --prefix frontend run build" -ForegroundColor Yellow
}
Write-Host "TraceGraph:     http://127.0.0.1:8000/app"
Write-Host "Press Ctrl+C to stop this run. If Neo4j remains active, use .\stop-neo4j.ps1."
Set-Location -LiteralPath $projectRoot
& $python -m uvicorn tracegraph.bootstrap:app --host 127.0.0.1 --port 8000
