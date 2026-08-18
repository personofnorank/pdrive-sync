#!/usr/bin/env bash
# update.sh — pull the latest pdrive-sync code and restart the daemon.
# Safe to re-run. Keeps local runtime state (data/, bin/, .venv/) intact.
set -euo pipefail
APP_DIR="$HOME/pdrive-sync-app"
cd "$APP_DIR"

echo "--- stopping daemon"
~/pdrive-sync-app/.venv/bin/python -m pdrive_sync.cli stop 2>/dev/null || true
# if it's mid deep-walk, SIGTERM can lag; give it a moment, then force
sleep 3
pid=$(systemctl --user show pdrive-sync.service -p MainPID --value 2>/dev/null || echo 0)
if [[ -n "$pid" && "$pid" != "0" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "--- daemon slow to stop; forcing"
    kill -9 "$pid" 2>/dev/null || true
    sleep 2
fi
systemctl --user reset-failed pdrive-sync.service 2>/dev/null || true

echo "--- pulling latest code"
git fetch origin
git reset --hard origin/main   # only touches git-tracked files; data/bin/.venv are gitignored

echo "--- ensuring deps"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q watchdog

echo "--- reinstalling systemd unit (picks up any unit changes)"
~/pdrive-sync-app/.venv/bin/python -m pdrive_sync.cli install

echo "--- starting daemon"
~/pdrive-sync-app/.venv/bin/python -m pdrive_sync.cli start

echo "done. status:"
~/pdrive-sync-app/.venv/bin/python -m pdrive_sync.cli status
