#!/bin/bash
# Polymarket AI Trading Agent - Runner Script
# Usage: ./run.sh [options]
#
# Options:
#   --once      Run a single cycle and exit
#   --status    Show current status only
#   --interval N  Run every N seconds (default: 300 = 5 minutes)

cd "$(dirname "$0")"

# Create data and logs directories
mkdir -p data logs

# Default: run continuously
if [ -z "$1" ]; then
    echo "Starting Polymarket AI Agent in continuous mode..."
    echo "Press Ctrl+C to stop"
    python3 agent.py --interval 300
else
    python3 agent.py "$@"
fi
