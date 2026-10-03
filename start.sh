#!/usr/bin/env bash
# Start FireCal in the background (keeps running after this terminal closes).
# Usage: ./start.sh        Stop: ./stop.sh        Log: data/server.log
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "FireCal needs Python 3.11 or newer"; exit 1; }
# a server already running this checkout's code is left alone; one running older code is restarted
if health=$(curl -fs "http://127.0.0.1:${PORT}/api/health" 2>/dev/null); then
  if grep -q "\"build\":\"$(python3 app/fingerprint.py)\"" <<<"$health"; then
    echo "FireCal is already running: http://127.0.0.1:${PORT}"
    exit 0
  fi
  echo "FireCal has changed since the server started: restarting it"
  ./stop.sh >/dev/null
fi
# the environment follows the requirements: set up on the first run, and rebuilt from scratch whenever they change
# (so packages that were dropped don't linger); a git checkout also gets the test tools (requirements-dev.txt)
reqs=requirements.txt; [ -d .git ] && reqs=requirements-dev.txt
want=$(cat requirements.txt "$reqs" | sha1sum | cut -c1-40)
if [ ! -x .venv/bin/uvicorn ] || [ "$(cat .venv/.requirements 2>/dev/null)" != "$want" ]; then
  echo "Setting up the Python environment…"
  rm -rf .venv.new && python3 -m venv .venv.new
  .venv.new/bin/pip install -q -r "$reqs"
  rm -rf .venv && mv .venv.new .venv
  # (a venv's scripts name its folder: point them at the final one)
  grep -rl "\.venv\.new" .venv/bin 2>/dev/null | xargs -r sed -i "s#$(pwd -P)/\.venv\.new#$(pwd -P)/.venv#g"
  echo "$want" > .venv/.requirements
fi
mkdir -p data
# keep the log from growing forever: roll it over past 5 MB (one previous copy is kept)
if [ -f data/server.log ] && [ "$(stat -c %s data/server.log)" -gt 5000000 ]; then
  mv -f data/server.log data/server.log.1
fi
setsid nohup .venv/bin/uvicorn app.main:app --port "${PORT}" >> data/server.log 2>&1 < /dev/null &
echo $! > data/server.pid
for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "FireCal is running: http://127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 1
done
echo "FireCal did not start; see data/server.log" >&2
exit 1
