#!/usr/bin/env bash
# Stop the FireCal server started by start.sh (waits until it has exited).
cd "$(dirname "$0")"
pids=$(cat data/server.pid 2>/dev/null; pgrep -f "[u]vicorn app.main:app")
pids=$(echo "$pids" | sort -u | xargs -r -n1 sh -c 'kill -0 "$0" 2>/dev/null && echo "$0"')
if [ -z "$pids" ]; then echo "FireCal was not running"; rm -f data/server.pid; exit 0; fi
kill $pids
for _ in $(seq 1 20); do
  alive=$(echo "$pids" | xargs -r -n1 sh -c 'kill -0 "$0" 2>/dev/null && echo "$0"')
  [ -z "$alive" ] && { rm -f data/server.pid; echo "FireCal stopped"; exit 0; }
  sleep 0.5
done
kill -9 $alive 2>/dev/null; rm -f data/server.pid; echo "FireCal stopped (forced)"
