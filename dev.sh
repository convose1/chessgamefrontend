#!/usr/bin/env bash
set -euo pipefail

# Starting port (override with PORT env)
PORT_START="${PORT:-8080}"
PORT="$PORT_START"

is_free() {
  # returns 0 if port is free
  ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

# Find a free port between PORT_START and PORT_START+50
LIMIT=$((PORT_START + 50))
while [ "$PORT" -le "$LIMIT" ]; do
  if is_free "$PORT"; then
    break
  fi
  PORT=$((PORT + 1))
done

if ! is_free "$PORT"; then
  echo "No free port found in range $PORT_START..$LIMIT" >&2
  exit 1
fi

export PORT
echo "$PORT" > .dev_port
echo "Starting server on http://127.0.0.1:$PORT"
exec python3 app.py
