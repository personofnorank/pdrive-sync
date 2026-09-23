#!/usr/bin/env bash
# update.sh — pull the latest pdrive-sync code and restart the daemon.
# Safe to re-run. Keeps local runtime state (data/, bin/) intact.
# Self-locating: works wherever the repo is cloned (no hardcoded path).
set -euo pipefail

# App dir = wherever this script lives (so a moved/renamed clone still works).
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
export PDRIVE_APP_DIR="$APP_DIR"
echo "--- app dir: $APP_DIR"

PY="$APP_DIR/.venv/bin/python"

echo "--- stopping daemon"
"$PY" -m pdrive_sync.cli stop 2>/dev/null || true
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

# --- venv: rebuild if missing OR stale (app dir was moved) -------------------
# A venv is NOT relocatable: its entry-point scripts hardcode the absolute
# interpreter path in their shebang, and the interpreter's sys.prefix points at
# the original location. After a `mv`, the venv breaks subtly (pip won't run,
# modules resolve wrong). Detect by comparing the venv's real path to APP_DIR
# and rebuild when they disagree.
need_venv=0
if [[ ! -x "$PY" ]]; then
    echo "--- no venv; creating"
    need_venv=1
elif [[ "$(cd "$APP_DIR/.venv" 2>/dev/null && pwd -P)" != "$APP_DIR/.venv" ]]; then
    echo "--- venv path mismatch (app moved); rebuilding"
    need_venv=1
elif ! "$PY" -c "import sys, os; sys.exit(0 if os.path.realpath(sys.prefix) == os.path.realpath('$APP_DIR/.venv') else 1)" 2>/dev/null; then
    echo "--- venv sys.prefix mismatch (app moved); rebuilding"
    need_venv=1
fi

if [[ "$need_venv" == "1" ]]; then
    rm -rf .venv
    python3 -m venv .venv
    PY="$APP_DIR/.venv/bin/python"
fi

echo "--- ensuring deps"
"$PY" -m pip install -q --upgrade pip watchdog pytest pygments packaging

echo "--- reinstalling systemd unit (picks up any unit/path changes)"
PDRIVE_APP_DIR="$APP_DIR" "$PY" -m pdrive_sync.cli install

echo "--- starting daemon"
"$PY" -m pdrive_sync.cli start

echo "done. status:"
"$PY" -m pdrive_sync.cli status
