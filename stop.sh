#!/usr/bin/env bash
# Stop the FireCal server started by start.sh.
pkill -f "uvicorn app.main:app" && echo "FireCal stopped" || echo "FireCal was not running"
