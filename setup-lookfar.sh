#!/usr/bin/env bash
# pdrive-sync setup for a fresh Linux machine (e.g. lookfar).
# Run with:  bash setup-lookfar.sh
# Idempotent — safe to re-run.

set -euo pipefail

APP_DIR="$HOME/pdrive-sync-app"
SYNC_ROOT="$HOME/pdrive"
CLI_BIN="$APP_DIR/bin/proton-drive"
VENV="$APP_DIR/.venv"

echo "=== pdrive-sync setup for $(hostname) ==="

# --- 1. dependencies -------------------------------------------------------
echo "--- checking dependencies"
need_apt=()
command -v python3 >/dev/null || need_apt+=(python3)
python3 -m venv --help >/dev/null 2>&1 || need_apt+=(python3-venv)
command -v notify-send >/dev/null || need_apt+=(libnotify-bin)
if ((${#need_apt[@]})); then
    echo "installing: ${need_apt[*]}"
    sudo apt-get update -qq && sudo apt-get install -y "${need_apt[@]}"
fi

# --- 2. Proton Drive CLI ---------------------------------------------------
if [[ ! -x "$CLI_BIN" ]]; then
    echo "--- downloading official Proton Drive CLI"
    mkdir -p "$APP_DIR/bin"
    # x64 build; use x64-baseline if the default crashes with 'Illegal instruction'
    url="https://proton.me/download/drive/cli/linux/x64/proton-drive"
    echo "  from $url"
    curl -fSL --progress-bar -o "$CLI_BIN" "$url" || {
        echo "  default build failed; trying baseline (no AVX2)"
        curl -fSL --progress-bar -o "$CLI_BIN" \
            "https://proton.me/download/drive/cli/linux/x64-baseline/proton-drive"
    }
    chmod +x "$CLI_BIN"
fi
echo "CLI: $("$CLI_BIN" --help >/dev/null 2>&1 && echo OK || echo 'present (run auth next)')"

# --- 3. app code -----------------------------------------------------------
# If you cloned/copied the repo here already, skip. Otherwise expect the
# pdrive_sync package to be present under $APP_DIR.
if [[ ! -d "$APP_DIR/pdrive_sync" ]]; then
    cat <<EOF

ERROR: $APP_DIR/pdrive_sync not found.

Copy the app from your other machine first, e.g. from auryn:
    rsync -a --exclude .venv --exclude data \\
        auryn:~/pdrive-sync-app/ ~/pdrive-sync-app/
then re-run this script.
EOF
    exit 1
fi

# --- 4. python venv + watchdog ---------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "--- creating venv"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip watchdog

# --- 5. user excludes (match the other machine) ----------------------------
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

# --- 6. put pdrive-sync on PATH ---------------------------------------------
mkdir -p "$HOME/.local/bin"
ln -sf "$APP_DIR/pdrive-sync" "$HOME/.local/bin/pdrive-sync"
if ! command -v pdrive-sync >/dev/null 2>&1; then
    echo "  note: ~/.local/bin not on PATH yet — add to your shell profile, e.g.:"
    echo '    echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> ~/.profile'
fi

# --- 7. authenticate -------------------------------------------------------
echo "--- authenticating with Proton (opens a browser; keep this terminal open)"
if "$CLI_BIN" filesystem list / -j >/dev/null 2>&1; then
    echo "  already authenticated"
else
    "$CLI_BIN" auth login
fi

# --- 7. install + baseline ---------------------------------------------------
cd "$APP_DIR"
"$VENV/bin/python" -m pdrive_sync.cli install

echo ""
echo "=== Next: establish the baseline (downloads your Drive to ~/pdrive) ==="
echo "This runs in no-delete mode and is resumable. It can take a long time"
echo "on a large drive. Run it in the foreground so you can watch:"
echo ""
echo "    pdrive-sync --no-delete resync -y"
echo ""
echo "Then start the background daemon (no-delete by default via pdrive-sync.env):"
echo "    pdrive-sync start"
echo ""
echo "Useful:  pdrive-sync resync-status | plan | logs | status"
