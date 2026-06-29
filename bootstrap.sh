#!/usr/bin/env bash
set -euo pipefail

PORT=8000
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

check_deps() {
    local missing=()
    for cmd in docker tailscale; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "Error: missing required tools: ${missing[*]}" >&2
        exit 1
    fi
}

funnel_url() {
    tailscale status --json \
        | jq -r '"https://" + (.Self.DNSName | rtrimstr("."))'
}

start() {
    echo "==> Building and starting container..."
    docker compose -f "$DIR/compose.yaml" up -d --build

    echo "==> Enabling Tailscale Funnel (443 -> localhost:$PORT)..."
    tailscale funnel --bg "$PORT"

    echo ""
    echo "Live at: $(funnel_url)"
}

stop() {
    echo "==> Stopping container..."
    docker compose -f "$DIR/compose.yaml" down

    echo "==> Disabling Tailscale Funnel..."
    tailscale funnel reset

    echo "Done."
}

check_deps

case "${1:-}" in
    --stop) stop ;;
    "")     start ;;
    *)
        echo "Usage: $0 [--stop]" >&2
        exit 1
        ;;
esac
