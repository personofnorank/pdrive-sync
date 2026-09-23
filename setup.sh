#!/usr/bin/env bash
# pdrive-sync setup for a fresh Linux machine. Idempotent — safe to re-run.
#
# Usage (from a clone of the git repo):
#   git clone git@github.com:personofnorank/pdrive-sync.git ~/pdrive-sync-app
#   bash ~/pdrive-sync-app/setup.sh
set -euo pipefail

# App dir = wherever this script lives (works for any clone location / home dir).
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_ROOT="${PDRIVE_SYNC_ROOT:-$HOME/pdrive}"
CLI_BIN="$APP_DIR/bin/proton-drive"
VENV="$APP_DIR/.venv"

echo "=== pdrive-sync setup for $(hostname) ==="

# --- dependencies -----------------------------------------------------------
echo "--- checking dependencies"
need_apt=()
command -v python3 >/dev/null || need_apt+=(python3)
python3 -m venv --help >/dev/null 2>&1 || need_apt+=(python3-venv)
command -v notify-send >/dev/null || need_apt+=(libnotify-bin)
if ((${#need_apt[@]})); then
    echo "installing: ${need_apt[*]}"
    sudo apt-get update -qq && sudo apt-get install -y "${need_apt[@]}"
fi

# --- Proton Drive CLI -------------------------------------------------------
if [[ ! -x "$CLI_BIN" ]]; then
    echo "--- downloading official Proton Drive CLI"
    mkdir -p "$APP_DIR/bin"
    # The download URL is versioned (…/cli/<ver>/linux-x64/proton-drive) and the
    # version changes per release, so parse the download page for the latest.
    ver=$(curl -fsSL --max-time 25 "https://proton.me/download/drive/cli/index.html" \
          | grep -oE 'cli/[0-9]+\.[0-9]+\.[0-9]+/' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' \
          | sort -V | tail -1)
    if [[ -z "$ver" ]]; then
        echo "  could not determine latest CLI version; falling back to 0.8.0"
        ver="0.8.0"
    fi
    echo "  latest CLI version: $ver"
    base="https://proton.me/download/drive/cli/$ver"
    curl -fSL --progress-bar -o "$CLI_BIN" "$base/linux-x64/proton-drive" || {
        echo "  default build failed; trying baseline (no AVX2)"
        curl -fSL --progress-bar -o "$CLI_BIN" "$base/linux-x64-baseline/proton-drive"
    }
    chmod +x "$CLI_BIN"
fi

# --- python venv + watchdog ---------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "--- creating venv"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip watchdog

# --- user excludes (only if none present) -------------------------------------
mkdir -p "$SYNC_ROOT"
if [[ ! -f "$SYNC_ROOT/.pdrive-ignore" ]]; then
    cat > "$SYNC_ROOT/.pdrive-ignore" <<'EOF'
# pdrive-sync excludes (fnmatch, one per line)
.stfolder
.pdrive-*
.sphinx
.doctrees
*_build
lib
lib64
site-packages
# Obsidian vault lives at ~/obsidian-vault (Syncthing) — never sync old path
code/obsidian
code/obsidian/*
EOF
fi

# --- pdrive-sync on PATH ------------------------------------------------------
mkdir -p "$HOME/.local/bin"
ln -sf "$APP_DIR/pdrive-sync" "$HOME/.local/bin/pdrive-sync"
command -v pdrive-sync >/dev/null 2>&1 || \
    echo "  note: add ~/.local/bin to PATH (e.g. in ~/.profile)"

# --- env file (explicit PDRIVE_APP_DIR so a non-default home-dir never trips it)
ENV_FILE="$APP_DIR/pdrive-sync.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
# pdrive-sync daemon environment.

# App directory (where pdrive_sync/ + bin/ live). Written by setup.sh so the
# daemon resolves correctly even when \$HOME differs from the account name.
PDRIVE_APP_DIR=$APP_DIR

# Set PDRIVE_NO_DELETE=1 to run the service in no-delete mode (apply
# everything except deletes). 0 = fully bidirectional.
PDRIVE_NO_DELETE=1

# Store the Proton session as a plaintext file (auth-session.json) instead of
# the OS keyring. This makes the daemon immune to keyring/PAM/graphical-session
# timing (the re-auth-on-reboot bug). Acceptable here because ~/ is on
# LUKS-encrypted disk and readable only by this user.
PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file
EOF
    echo "  wrote $ENV_FILE (PDRIVE_NO_DELETE=1, file creds store, PDRIVE_APP_DIR=$APP_DIR)"
fi

# --- authenticate -------------------------------------------------------------
# Uses the file-based session store (keyring-independent) so the daemon
# survives reboots without a re-login.
echo "--- authenticating with Proton (opens a browser; keep this terminal open)"
if PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file "$CLI_BIN" filesystem list / -j >/dev/null 2>&1; then
    echo "  already authenticated"
else
    PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file "$CLI_BIN" auth login
fi

# --- install systemd unit ------------------------------------------------------
cd "$APP_DIR"
"$VENV/bin/python" -m pdrive_sync.cli install

cat <<'EOF'

=== Next: establish the baseline (downloads your Drive to ~/pdrive) ===
Resumable; runs in no-delete mode by default. Watch it in the foreground:

    pdrive-sync --no-delete resync -y

Then start the background daemon:

    pdrive-sync start

Useful:  pdrive-sync resync-status | plan | logs | status
Update later with:  run update.sh from the app dir (wherever you cloned it)
EOF
