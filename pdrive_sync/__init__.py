"""Configuration for pdrive-sync."""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------
APP_DIR = Path(os.environ.get("PDRIVE_APP_DIR", Path.home() / "pdrive-sync-app")).resolve()
SYNC_ROOT = Path(os.environ.get("PDRIVE_SYNC_ROOT", Path.home() / "pdrive")).resolve()
REMOTE_ROOT = os.environ.get("PDRIVE_REMOTE_ROOT", "/my-files")
DATA_DIR = Path(os.environ.get("PDRIVE_DATA_DIR", APP_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "state.db"
LOG_PATH = DATA_DIR / "pdrive-sync.log"
LOCK_PATH = DATA_DIR / "pdrive-sync.lock"

# The official Proton Drive CLI binary.
CLI = os.environ.get("PDRIVE_CLI", str(Path.home() / "Downloads" / "proton-drive"))

# --- Behaviour ---------------------------------------------------------------
# How often (seconds) the daemon polls the remote tree for changes when idle.
POLL_INTERVAL = float(os.environ.get("PDRIVE_POLL_INTERVAL", "60"))
# Debounce window for local inotify events before a sync cycle is triggered.
DEBOUNCE_SECONDS = float(os.environ.get("PDRIVE_DEBOUNCE", "2.0"))
# Safety cap: abort a reconcile cycle if it would delete more than this many
# files (local + remote combined) unless force is set. rclone --max-delete style.
MAX_DELETE = int(os.environ.get("PDRIVE_MAX_DELETE", "50"))
# Max size (bytes) for which we compute SHA1 equality checks on the fast path.
HASH_COMPARE_LIMIT = int(os.environ.get("PDRIVE_HASH_LIMIT", str(512 * 1024 * 1024)))

# Paths (relative to SYNC_ROOT) that are never synced. fnmatch-style patterns.
DEFAULT_EXCLUDES = [
    ".pdrive-sync-tmp*",
    "*.conflict-*",
    ".Trash*",
    "lost+found",
]

CONFLICT_SUFFIX_FMT = ".conflict-%Y%m%d-%H%M%S"

# Desktop notifications (matches the mattermost_post.py DBUS trick that works
# on this COSMIC/Wayland setup).
NOTIFY = os.environ.get("PDRIVE_NOTIFY", "1") == "1"


def ensure_dirs() -> None:
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
