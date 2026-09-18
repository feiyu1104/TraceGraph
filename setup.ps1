param(
    [switch]$SkipDemoData,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

function Find-Python311 {
    $candidates = @(
        @{ Command = "py"; Prefix = @("-3") },
        @{ Command = "python"; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $prefix = @($candidate.Prefix)
        $versionText = & $command.Source @prefix -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        if ([version]$versionText.Trim() -ge [version]"3.11") {
            return [PSCustomObject]@{ Command = $command.Source; Prefix = $prefix }
        }
    }
    throw "TraceGraph requires Python 3.11 or newer. Install Python, then run this script again."
}

function Restore-EnvironmentValue([string]$Name, [object]$Value) {
    if ($null -eq $Value) {
        Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    } else {
        Set-Item -LiteralPath "Env:$Name" -Value ([string]$Value)
    }
}

Write-Host "[1/5] Preparing local configuration"
$envFile = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ".env.example") -Destination $envFile
    Write-Host "Created .env with the offline SQLite defaults."
} else {
    Write-Host "Keeping the existing .env file."
}

Write-Host "[2/5] Preparing Python"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Find-Python311
    $launcherArgs = @($launcher.Prefix)
    & $launcher.Command @launcherArgs -m venv (Join-Path $projectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python virtual environment." }
}
$venvVersion = & $venvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0 -or [version]$venvVersion.Trim() -lt [version]"3.11") {
    throw "The existing .venv does not use Python 3.11 or newer. Remove .venv and run this script again."
}

$pythonMarker = Join-Path $projectRoot ".venv\.tracegraph-installed"
$pyproject = Get-Item -LiteralPath (Join-Path $projectRoot "pyproject.toml")
$needsPythonInstall = -not (Test-Path -LiteralPath $pythonMarker)
if (-not $needsPythonInstall) {
    $needsPythonInstall = $pyproject.LastWriteTimeUtc -gt (Get-Item -LiteralPath $pythonMarker).LastWriteTimeUtc
}
if ($needsPythonInstall) {
    & $venvPython -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw "Could not install the Python dependencies." }
    New-Item -ItemType File -Path $pythonMarker -Force | Out-Null
} else {
    Write-Host "Python dependencies are already installed."
}

Write-Host "[3/5] Preparing the web interface"
$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
if (-not $node -or -not $npm) {
    throw "TraceGraph requires Node.js 18 or newer (including npm). Install Node.js, then run this script again."
}
$nodeVersion = (& $node.Source --version).Trim().TrimStart("v")
if ([int]$nodeVersion.Split(".")[0] -lt 18) {
    throw "TraceGraph requires Node.js 18 or newer. Found Node.js $nodeVersion."
}
$frontendRoot = Join-Path $projectRoot "frontend"
$lockFile = Get-Item -LiteralPath (Join-Path $frontendRoot "package-lock.json")
$npmMarkerPath = Join-Path $frontendRoot "node_modules\.package-lock.json"
$needsNpmInstall = -not (Test-Path -LiteralPath $npmMarkerPath)
if (-not $needsNpmInstall) {
    $needsNpmInstall = $lockFile.LastWriteTimeUtc -gt (Get-Item -LiteralPath $npmMarkerPath).LastWriteTimeUtc
}
if ($needsNpmInstall) {
    & $npm.Source --prefix $frontendRoot ci
    if ($LASTEXITCODE -ne 0) { throw "Could not install the frontend dependencies." }
} else {
    Write-Host "Frontend dependencies are already installed."
}
& $npm.Source --prefix $frontendRoot run build
if ($LASTEXITCODE -ne 0) { throw "Could not build the web interface." }

Write-Host "[4/5] Loading the bundled DUTMed demo"
if (-not $SkipDemoData) {
    $demoSource = Join-Path $projectRoot "examples\data\dutmed-demo.jsonl"
    if (-not (Test-Path -LiteralPath $demoSource)) { throw "Bundled DUTMed demo data is missing: $demoSource" }
    $previousBackend = $env:TRACEGRAPH_GRAPH_BACKEND
    $previousDatabase = $env:TRACEGRAPH_DATABASE
    try {
        # The first-run demo is always available without Java or an external database.
        $env:TRACEGRAPH_GRAPH_BACKEND = "sqlite"
        $env:TRACEGRAPH_DATABASE = "data/local/tracegraph.db"
        & $venvPython -m tracegraph.cli import-medical --source $demoSource --database "data/local/tracegraph.db"
        if ($LASTEXITCODE -ne 0) { throw "Could not import the bundled DUTMed demo data." }
    } finally {
        Restore-EnvironmentValue "TRACEGRAPH_GRAPH_BACKEND" $previousBackend
        Restore-EnvironmentValue "TRACEGRAPH_DATABASE" $previousDatabase
    }
} else {
    Write-Host "Demo data import was skipped."
}

Write-Host "[5/5] Setup complete" -ForegroundColor Green
if ($NoStart) {
    Write-Host "Run .\start.ps1 -Mode sqlite when you are ready."
    exit 0
}

Write-Host "Starting TraceGraph. Open http://127.0.0.1:8000/app"
& (Join-Path $projectRoot "start.ps1") -Mode sqlite
