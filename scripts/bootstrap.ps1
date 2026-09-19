<#
.SYNOPSIS
    Bring a clean Windows machine to a working AxonFDE development environment.

.DESCRIPTION
    Checks prerequisites, creates .env, installs dependencies, starts the core
    containers, waits for them to become healthy, and runs the test suite.

    Safe to re-run: every step is idempotent.

.EXAMPLE
    .\scripts\bootstrap.ps1
    .\scripts\bootstrap.ps1 -SkipTests
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipContainers
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "    OK  $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "    !   $Message" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
Write-Step "Checking prerequisites"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not installed. Install it from https://docs.astral.sh/uv/ and re-run."
}
Write-Ok "uv $((uv --version) -replace 'uv ', '')"

if (-not $SkipContainers) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker is not installed. Install Docker Desktop and re-run."
    }
    docker info 2>&1 | Out-Null
    if (-not $?) {
        throw "The Docker daemon is not running. Start Docker Desktop and re-run."
    }
    Write-Ok "Docker daemon reachable"
}

# The repository must not live inside a synced folder. OneDrive will fight
# .venv, node_modules and Docker bind mounts, producing file locks and
# intermittent corruption that are tedious to diagnose.
foreach ($marker in @('OneDrive', 'Dropbox', 'Google Drive', 'iCloud')) {
    if ($repoRoot -like "*$marker*") {
        Write-Warn "This repository is inside a $marker folder ($repoRoot)."
        Write-Warn "Cloud sync will corrupt virtual environments and container mounts."
        Write-Warn "Move it somewhere like C:\dev\axonfde before continuing."
    }
}

# ---------------------------------------------------------------------------
Write-Step "Preparing configuration"

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Ok "Created .env from .env.example"
} else {
    Write-Ok ".env already exists (left untouched)"
}

# ---------------------------------------------------------------------------
Write-Step "Installing dependencies"
uv sync
if (-not $?) { throw "uv sync failed" }
Write-Ok "Virtual environment ready"

# ---------------------------------------------------------------------------
if (-not $SkipContainers) {
    Write-Step "Starting core services (postgres, sql server, minio)"
    docker compose --profile core up -d
    if (-not $?) { throw "docker compose up failed" }

    Write-Host "    Waiting for containers to report healthy..." -ForegroundColor DarkGray
    # SQL Server needs roughly 45 seconds on a cold start.
    $deadline = (Get-Date).AddMinutes(4)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        $states = docker compose --profile core ps --format '{{.Service}}={{.Health}}'
        if ($states -and ($states -notmatch 'starting') -and ($states -notmatch 'unhealthy')) {
            $healthy = $true
            break
        }
        Start-Sleep -Seconds 5
    }

    if (-not $healthy) {
        docker compose --profile core ps
        throw "Containers did not become healthy within 4 minutes. See the status above."
    }
    Write-Ok "All core services healthy"
}

# ---------------------------------------------------------------------------
if (-not $SkipTests) {
    Write-Step "Running the quality gate"
    uv run poe check
    if (-not $?) { throw "poe check failed" }
    Write-Ok "Lint, types, module contracts and tests all pass"
}

# ---------------------------------------------------------------------------
Write-Host "`nAxonFDE is ready." -ForegroundColor Green
Write-Host @"

  Next steps
  ----------
  uv run poe api      Start the API on http://localhost:8000 (docs at /docs)
  uv run poe check    Lint, typecheck, module contracts and tests
  uv run poe demo     End-to-end incident walkthrough        (Phase 1)
  uv run poe bench    Run AxonBench                          (Phase 1)
  uv run poe down     Stop all services

  No API key is required: the default LLM mode replays recorded cassettes,
  so tests and the demo run offline and cost nothing.
"@ -ForegroundColor Gray
