$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $projectRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match "^\s*([^#][^=]*)=(.*)$") {
            Set-Item -LiteralPath "Env:$($matches[1].Trim())" -Value $matches[2].Trim()
        }
    }
}
$javaHome = if ($env:JAVA_HOME) {
    $env:JAVA_HOME
} else {
    [Environment]::GetEnvironmentVariable("JAVA_HOME", "User")
}
$expectedJava = (Resolve-Path -LiteralPath (Join-Path $javaHome "bin\java.exe")).Path
$processes = Get-Process -Name "java" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Path -and [string]::Equals(
            $_.Path,
            $expectedJava,
            [StringComparison]::OrdinalIgnoreCase
        )
    }
if (-not $processes) {
    Write-Host "Neo4j is not running."
    exit 0
}

Stop-Process -Id $processes.Id
Write-Host "Neo4j stopped."
