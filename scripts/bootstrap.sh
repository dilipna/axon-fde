#!/usr/bin/env bash
#
# Bring a clean macOS/Linux machine to a working AxonFDE development
# environment: prerequisites, .env, dependencies, containers, tests.
#
# Safe to re-run: every step is idempotent.
#
# Usage:
#   ./scripts/bootstrap.sh
#   SKIP_TESTS=1 ./scripts/bootstrap.sh
#   SKIP_CONTAINERS=1 ./scripts/bootstrap.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BLUE='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; GRAY='\033[0;90m'; NC='\033[0m'
step() { printf "\n${BLUE}==> %s${NC}\n" "$1"; }
ok()   { printf "    ${GREEN}OK${NC}  %s\n" "$1"; }
warn() { printf "    ${YELLOW}!${NC}   %s\n" "$1"; }

# ---------------------------------------------------------------------------
step "Checking prerequisites"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it from https://docs.astral.sh/uv/ and re-run." >&2
  exit 1
fi
ok "uv $(uv --version | sed 's/uv //')"

if [[ "${SKIP_CONTAINERS:-0}" != "1" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed. Install Docker and re-run." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "The Docker daemon is not running. Start it and re-run." >&2
    exit 1
  fi
  ok "Docker daemon reachable"
fi

# Cloud-synced folders fight virtual environments and container bind mounts.
for marker in OneDrive Dropbox "Google Drive" iCloud; do
  if [[ "$REPO_ROOT" == *"$marker"* ]]; then
    warn "This repository is inside a $marker folder ($REPO_ROOT)."
    warn "Cloud sync will corrupt virtual environments and container mounts."
    warn "Move it somewhere like ~/dev/axonfde before continuing."
  fi
done

# ---------------------------------------------------------------------------
step "Preparing configuration"

if [[ ! -f .env ]]; then
  cp .env.example .env
  ok "Created .env from .env.example"
else
  ok ".env already exists (left untouched)"
fi

# ---------------------------------------------------------------------------
step "Installing dependencies"
uv sync
ok "Virtual environment ready"

# ---------------------------------------------------------------------------
if [[ "${SKIP_CONTAINERS:-0}" != "1" ]]; then
  step "Starting core services (postgres, sql server, minio)"
  docker compose --profile core up -d

  printf "${GRAY}    Waiting for containers to report healthy...${NC}\n"
  # SQL Server needs roughly 45 seconds on a cold start.
  deadline=$(( SECONDS + 240 ))
  healthy=0
  while (( SECONDS < deadline )); do
    states="$(docker compose --profile core ps --format '{{.Service}}={{.Health}}' || true)"
    if [[ -n "$states" ]] && ! grep -qE 'starting|unhealthy' <<<"$states"; then
      healthy=1
      break
    fi
    sleep 5
  done

  if (( healthy != 1 )); then
    docker compose --profile core ps
    echo "Containers did not become healthy within 4 minutes. See the status above." >&2
    exit 1
  fi
  ok "All core services healthy"
fi

# ---------------------------------------------------------------------------
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  step "Running the quality gate"
  uv run poe check
  ok "Lint, types, module contracts and tests all pass"
fi

# ---------------------------------------------------------------------------
printf "\n${GREEN}AxonFDE is ready.${NC}\n"
cat <<'EOF'

  Next steps
  ----------
  uv run poe api      Start the API on http://localhost:8000 (docs at /docs)
  uv run poe check    Lint, typecheck, module contracts and tests
  uv run poe demo     End-to-end incident walkthrough        (Phase 1)
  uv run poe bench    Run AxonBench                          (Phase 1)
  uv run poe down     Stop all services

  No API key is required: the default LLM mode replays recorded cassettes,
  so tests and the demo run offline and cost nothing.
EOF
