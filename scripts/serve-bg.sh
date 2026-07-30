#!/bin/bash
# Start the web page in the background and return immediately.
#
# Why this exists: running `manage.py serve` or `demo` directly blocks the
# terminal until the server exits, which it never does. From Claude Code's `!`
# prompt that wedges the session, and Escape — the only way out — kills the
# server. Running it in a second window is no better: clicking that window to
# focus it dismisses any open prompt in the session and registers a rejection.
#
# So: background it, log it, get the prompt back. Nothing to type but a path.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8080}"
LOG="${TMPDIR:-/tmp}/campgroundfinder-$PORT.log"
MODE="${1:-demo}"          # `demo` seeds mock availability; `serve` uses the real DB

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
    echo "note: no .venv found, using $PY (a YAML config will need PyYAML)"
fi

# Clear out a server left over from a previous run, so the port is free and we
# never end up with two processes sharing one database.
if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
    echo "stopping the server already on port $PORT"
    lsof -ti "tcp:$PORT" | xargs kill
    sleep 1
fi

cd "$ROOT" || exit 1
nohup "$PY" scripts/manage.py "$MODE" --port "$PORT" >"$LOG" 2>&1 &
PID=$!

# Wait for it to actually answer, rather than claiming success and leaving a
# crash to be discovered in the browser.
for _ in $(seq 1 30); do
    if curl -fsS -m 1 -o /dev/null "http://127.0.0.1:$PORT/"; then
        echo "CampgroundFinder is up:  http://127.0.0.1:$PORT"
        echo "  pid $PID   log $LOG"
        echo "  stop it with:  bash $ROOT/scripts/stop-bg.sh"
        exit 0
    fi
    sleep 1
done

echo "the server did not come up within 30s — last lines of $LOG:"
tail -20 "$LOG"
exit 1
