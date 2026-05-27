#!/bin/bash
# Polymarket AI Trading Agent - Runner Script
# Usage: ./run.sh [options]
#
# Options:
#   --once      Run a single cycle and exit
#   --status    Show current status only
#   --interval N  Run every N seconds (default: 300 = 5 minutes)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p data logs

if [ -z "$1" ]; then
    echo "Starting Polymarket AI Agent in continuous mode..."
    echo "Press Ctrl+C to stop"
    python3 -m src.core.agent --interval 300
else
    python3 -m src.core.agent "$@"
fi
