#!/usr/bin/env bash
# PitchBot launcher — starts the server and opens the browser.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=8050
LOG="$SCRIPT_DIR/logs/pitchbot.log"
PID_FILE="$SCRIPT_DIR/logs/pitchbot.pid"
mkdir -p "$SCRIPT_DIR/logs"

# ── Kill any previous instance ───────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping previous PitchBot instance (PID $OLD_PID)…"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# ── Activate virtualenv if present ──────────────────────────────────────────
if [ -d "$SCRIPT_DIR/venv" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
fi

# ── Start app server ─────────────────────────────────────────────────────────
echo "Starting PitchBot on http://localhost:$PORT …"
python "$SCRIPT_DIR/app.py" >> "$LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# ── Wait for server to accept connections ────────────────────────────────────
for i in $(seq 1 15); do
    if curl -sf "http://localhost:$PORT" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# ── Open browser ─────────────────────────────────────────────────────────────
URL="http://localhost:$PORT"
if command -v xdg-open > /dev/null 2>&1; then
    xdg-open "$URL"
elif command -v open > /dev/null 2>&1; then
    open "$URL"
else
    echo "Open $URL in your browser."
fi

echo "PitchBot running (PID $SERVER_PID). Logs: $LOG"
