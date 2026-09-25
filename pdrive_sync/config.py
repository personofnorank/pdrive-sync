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

# The official Proton Drive CLI binary (moved out of ~/Downloads, which is
# periodically cleared, into the app dir).
CLI = os.environ.get("PDRIVE_CLI", str(APP_DIR / "bin" / "proton-drive"))

# --- Behaviour ---------------------------------------------------------------
POLL_INTERVAL = float(os.environ.get("PDRIVE_POLL_INTERVAL", "60"))
# Full (recursive) remote walk cadence, in seconds. The pruned walk runs every
# POLL_INTERVAL; the deep walk is the correctness backstop that catches remote
# edits whose parent-folder mtimes didn't change.
DEEP_INTERVAL = float(os.environ.get("PDRIVE_DEEP_INTERVAL", "1800"))
DEBOUNCE_SECONDS = float(os.environ.get("PDRIVE_DEBOUNCE", "2.0"))
MAX_DELETE = int(os.environ.get("PDRIVE_MAX_DELETE", "50"))
# A single reconcile pass must not be able to emit an unbounded number of
# conflict copies: on 2026-08-20 one pass wrote 71,717 copies (342 GiB) and
# silently filled the disk, because conflict copies are excluded from sync and
# nothing ever cleans them up. Exceeding the cap aborts the pass instead.
MAX_CONFLICTS = int(os.environ.get("PDRIVE_MAX_CONFLICTS", "200"))
HASH_COMPARE_LIMIT = int(os.environ.get("PDRIVE_HASH_LIMIT", str(512 * 1024 * 1024)))

DEFAULT_EXCLUDES = [
    ".pdrive-sync-tmp*",
    "*.conflict-*",
    ".Trash*",
    "lost+found",
]

# Name of an optional exclude file the user can drop in the sync root
# (rclone/rsync style, one fnmatch pattern per line, '#' comments).
EXCLUDE_FILE = ".pdrive-ignore"

# Directories that are essentially always regenerable build artifacts. These
# are matched by NAME at any depth. 13k-folder trees are mostly this stuff and
# syncing it is what makes a naive baseline take hours.
DEFAULT_DIR_EXCLUDES = [
    "node_modules", "__pycache__", ".git", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist-info",
    # generated doc/site output — huge, regenerable
    "_build", "build", "dist", ".next", ".cache", "target",
]

CONFLICT_SUFFIX_FMT = ".conflict-%Y%m%d-%H%M%S"
# Scratch directory used while fetching the remote side of a conflict. Matches
# an entry in DEFAULT_EXCLUDES so it is never itself synced.
CONFLICT_TMP_PREFIX = ".pdrive-sync-tmp-conflict-"
NOTIFY = os.environ.get("PDRIVE_NOTIFY", "1") == "1"


def ensure_dirs() -> None:
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
