#!/bin/bash
# Restart watchdog if heartbeat is stale (> 10 min old) or process dead.
# Add to crontab: */5 * * * * /home/darren/share/A05_polymarket/0x8-polymarket/src/scripts/watchdog_monitor.sh

WATCHDOG_PATTERN="stop_loss_watchdog"
HEARTBEAT_FILE="/tmp/watchdog_heartbeat"
MAX_AGE=600  # 10 minutes
LOG="/tmp/watchdog_monitor.log"
PROJECT_ROOT="/home/darren/share/A05_polymarket/0x8-polymarket"

ts() { date '+%H:%M:%S'; }

# Check if process is running
if ! pgrep -f "$WATCHDOG_PATTERN" > /dev/null 2>&1; then
    echo "[$(ts)] Watchdog NOT RUNNING — restarting" >> "$LOG"
    cd "$PROJECT_ROOT" && setsid python3 -m src.execution.stop_loss_watchdog >> /tmp/stop_loss.log 2>&1 &
    echo "[$(ts)] Started PID $!" >> "$LOG"
    exit 0
fi

# Check heartbeat age
if [ -f "$HEARTBEAT_FILE" ]; then
    HEARTBEAT_TS=$(python3 -c "import json; print(json.load(open('$HEARTBEAT_FILE')).get('ts', 0))" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    AGE=$((NOW - ${HEARTBEAT_TS%.*}))
    
    if [ "$AGE" -gt "$MAX_AGE" ]; then
        echo "[$(ts)] Heartbeat STALE (${AGE}s) — killing and restarting" >> "$LOG"
        pkill -9 -f "$WATCHDOG_PATTERN"
        sleep 2
        cd "$PROJECT_ROOT" && setsid python3 -m src.execution.stop_loss_watchdog >> /tmp/stop_loss.log 2>&1 &
        echo "[$(ts)] Restarted PID $!" >> "$LOG"
    fi
else
    echo "[$(ts)] No heartbeat file — restarting" >> "$LOG"
    pkill -9 -f "$WATCHDOG_PATTERN" 2>/dev/null
    sleep 2
    cd "$PROJECT_ROOT" && setsid python3 -m src.execution.stop_loss_watchdog >> /tmp/stop_loss.log 2>&1 &
    echo "[$(ts)] Started PID $!" >> "$LOG"
fi
