#!/usr/bin/env bash
# Stop the FireCal server started by start.sh from THIS folder (waits until it has exited).
cd "$(dirname "$0")"
here=$(pwd -P)
mine() {  # FireCal servers whose working directory is this folder (never another copy's)
  for pid in $(cat data/server.pid 2>/dev/null) $(pgrep -f "[u]vicorn app.main:app"); do
    [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" = "$here" ] && echo "$pid"
  done | sort -u
}
pids=$(mine)
if [ -z "$pids" ]; then echo "FireCal was not running"; rm -f data/server.pid; exit 0; fi
kill $pids
for _ in $(seq 1 20); do
  [ -z "$(mine)" ] && { rm -f data/server.pid; echo "FireCal stopped"; exit 0; }
  sleep 0.5
done
kill -9 $(mine) 2>/dev/null; rm -f data/server.pid; echo "FireCal stopped (forced)"
