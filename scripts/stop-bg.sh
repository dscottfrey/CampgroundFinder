#!/bin/bash
# Stop the backgrounded web page. Companion to serve-bg.sh.
set -u
PORT="${PORT:-8080}"

if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
    lsof -ti "tcp:$PORT" | xargs kill
    echo "stopped the server on port $PORT"
else
    echo "nothing was running on port $PORT"
fi
