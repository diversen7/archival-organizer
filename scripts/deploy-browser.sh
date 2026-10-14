#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/azks430/archival-organizer"
APP_BIN="$APP_DIR/.venv/bin/archival-browser"
LOG_FILE="/home/azks430/archival-browser.log"

echo "==> Updating repository..."
cd "$APP_DIR"
git switch main
git pull --ff-only origin main

echo "==> Syncing dependencies from uv.lock..."
uv sync --locked

echo "==> Stopping archival-browser..."
# Match the executable path even when Python precedes it in the command line.
PROCESS_PATTERN="$APP_BIN( |$)"
pkill -f "$PROCESS_PATTERN" || true

while pgrep -f "$PROCESS_PATTERN" > /dev/null; do
    sleep 0.5
done

echo "==> Starting archival-browser..."
nohup "$APP_BIN" \
    /home/azks430/melica-output \
    --input /mnt/data2/melica-png \
    --host 0.0.0.0 \
    --port 8002 \
    > "$LOG_FILE" 2>&1 < /dev/null &
PID=$!

# Catch immediate failures; this checks process liveness, not HTTP readiness.
sleep 2
if ! kill -0 "$PID" 2>/dev/null; then
    echo "==> Startup failed:"
    tail -n 30 "$LOG_FILE"
    exit 1
fi

echo "==> Browser process running (PID: $PID)"
