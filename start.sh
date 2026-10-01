#!/usr/bin/env bash
# Start FireCal in the background (keeps running after this terminal closes).
# Usage: ./start.sh        Stop: ./stop.sh        Log: data/server.log
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
if curl -fs "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  echo "FireCal is already running: http://127.0.0.1:${PORT}"
  exit 0
fi
if [ ! -x .venv/bin/uvicorn ]; then
  echo "Setting up the Python environment (first run only)…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
mkdir -p data
setsid nohup .venv/bin/uvicorn app.main:app --port "${PORT}" >> data/server.log 2>&1 < /dev/null &
for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "FireCal is running: http://127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 1
done
echo "FireCal did not start; see data/server.log" >&2
exit 1
