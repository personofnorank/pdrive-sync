# pdrive-sync — bidirectional Proton Drive sync for Linux

A background daemon that keeps a local folder (`~/pdrive`) in two-way sync with
your Proton Drive (`/my-files`), using the **official Proton Drive CLI** for all
transfers (so encryption, auth, and uploads stay Proton-supported) and a
**rclone-bisync-style 3-way reconcile** for correctness.

Built because the official clients have no Linux real-time sync, the CLI is
one-shot only, and community options are either upload-only
(DamianB-BitFlipper/proton-drive-sync) or unmaintained (rclone's Proton backend).

## How it works

For every path it compares three states:

| | |
|---|---|
| **L** | current local filesystem (`~/pdrive`, watched via inotify) |
| **R** | current remote tree (Proton Drive, via the CLI) |
| **S** | last-synced snapshot (SQLite at `$PDRIVE_APP_DIR/data/state.db`) |

and derives an action:

| Situation | Action |
|---|---|
| Local changed, remote unchanged | **upload** |
| Remote changed, local unchanged | **download** |
| Both changed | **conflict** — keep both, loser renamed `name.conflict-YYYYMMDD-HHMMSS.ext` (rclone `--conflict-loser pathname`) |
| Present locally + in snapshot, gone remotely | remote deleted it → **delete local** |
| Present remotely + in snapshot, gone locally | local deleted it → **trash remote** |
| New on one side only | copy to the other side |

**Integrity:** Proton's claimed SHA1 is verified after download; uploads are
skipped when the remote SHA1 already matches. mtime-only shifts (e.g. after a
reboot) don't trigger re-syncs — a SHA1 fallback confirms content is unchanged.

**Safety:**
- deletes go to Proton **trash** (recoverable), never hard-delete
- a reconcile cycle that would delete more than `MAX_DELETE` files (default 50)
  **aborts the deletes** and sends a desktop notification (rclone `--max-delete`)
- a single-instance lock prevents two daemons fighting over one sync root
- local paths are validated to stay under the sync root (no symlink escape)

**Remote-change detection (the part Proton makes hard):** the CLI's recursive
listing is slow (~3s/folder) and Proton asks clients not to poll recursively, so
the daemon uses an **adaptive** strategy —
- **local → remote: instant** (inotify, ~2s debounce)
- **remote → local:** every `POLL_INTERVAL` (60s) it does a cheap root
  fingerprint + an mtime-pruned walk of changed folders; a **full** deep walk
  runs every `DEEP_INTERVAL` (30 min) as the correctness backstop.

## Setup

```bash
cd <pdrive-sync-app>   # wherever you cloned it
./pdrive-sync resync      # one-off baseline: reconcile both trees (resumable)
./pdrive-sync install     # install + enable the systemd --user service
./pdrive-sync start       # start background sync
```

## Commands

| command | what it does |
|---|---|
| `pdrive-sync resync [-y]` | rebuild the baseline, one top-level folder at a time (resumable) |
| `pdrive-sync sync` | one foreground reconcile cycle, then exit |
| `pdrive-sync start` / `stop` | start/stop the background service |
| `pdrive-sync status` | service state + tracked-path count |
| `pdrive-sync logs [-n N]` | tail the log |
| `pdrive-sync conflicts` | list unresolved `*.conflict-*` copies |
| `pdrive-sync install` / `uninstall` | manage the systemd user unit |
| `pdrive-sync run` | run the daemon in the foreground (debugging) |

`--force` (global) bypasses the `MAX_DELETE` safety cap.

## Excluding paths

Regenerable build dirs (`node_modules`, `_build`, `.venv`, `__pycache__`, …) are
excluded by default — syncing them is what makes a naive baseline take hours.
Add your own patterns (fnmatch, one per line, `#` comments) to:

```
~/pdrive/.pdrive-ignore
```

## Configuration (environment variables)

| var | default | meaning |
|---|---|---|
| `PDRIVE_SYNC_ROOT` | `~/pdrive` | local sync root |
| `PDRIVE_REMOTE_ROOT` | `/my-files` | remote root |
| `PDRIVE_CLI` | `$PDRIVE_APP_DIR/bin/proton-drive` | path to the official CLI |
| `PDRIVE_APP_DIR` | `~/pdrive-sync-app` | where the app (package + `bin/`) lives |
| `PDRIVE_POLL_INTERVAL` | `60` | seconds between remote checks |
| `PDRIVE_DEEP_INTERVAL` | `1800` | seconds between full remote walks |
| `PDRIVE_MAX_DELETE` | `50` | delete safety cap per cycle |
| `PDRIVE_NOTIFY` | `1` | desktop notifications on/off |

### Per-machine env file (`pdrive-sync.env`) — NOT in git

The systemd unit reads `$PDRIVE_APP_DIR/pdrive-sync.env` for `PDRIVE_APP_DIR`,
`PDRIVE_NO_DELETE`, and `PROTON_DRIVE_CREDENTIALS_STORE`. **This file is
gitignored and generated per machine** by `setup.sh`, because `PDRIVE_APP_DIR`
is machine-specific — the app may live under a non-default home dir (e.g.
`/home/jake` on one laptop vs `/home/jake.hoban@canonical.com` on another).
Versioning it would cross-break machines on `git pull`: a pull would overwrite
the correct local path with the other machine's. `setup.sh` derives `APP_DIR`
from its own location, so it always writes the right path for the machine it
runs on.

If you edit `pdrive-sync.env`, the change is local-only — edit it again on each
machine. Shared defaults (`PDRIVE_NO_DELETE`, the creds store) live in the file
but can diverge per machine if needed.

## Layout

```
<pdrive-sync-app>/   (wherever you cloned it — e.g. ~/pdrive/code/pdrive-sync)
  pdrive-sync            # CLI wrapper (this is what you run)
  pdrive-sync.env        # per-machine env (PDRIVE_APP_DIR, …) — NOT in git
  pdrive_sync/           # the Python package
    config.py  state.py  local.py  proton.py  reconcile.py  daemon.py  cli.py
  .venv/                 # dedicated Python env (watchdog)
  data/                  # state.db, pdrive-sync.log, lock
```

### Moving the app dir

The app dir is relocatable, but a plain `mv` needs four follow-ups (or it'll
break): update the `APP_DIR` in the `pdrive-sync` wrapper, repoint the
`~/.local/bin/pdrive-sync` symlink, regenerate the systemd unit
(`PDRIVE_APP_DIR=<new> .venv/bin/python -m pdrive_sync.cli install`), and
**rebuild the venv** (`rm -rf .venv && python3 -m venv .venv && .venv/bin/pip
install watchdog pytest pygments packaging`) — the venv's entry-point scripts
hardcode the old absolute path in their shebangs and won't survive a move. If
the new location is *inside* the synced tree (e.g. `~/pdrive/code/...`), also
add it to `.pdrive-ignore` so its venv/state/binary don't churn through the
sync.


## Notes / limits

- **First baseline is slow** if you have hundreds of GB: every file's SHA1 is
  computed once to establish the integrity-checked snapshot (same as rclone's
  first `bisync`). It's resumable — re-run `resync` and it skips done folders.
- Remote→local edits land within ~1 min (root-level) to ~30 min (deep), because
  the CLI exposes no event stream. Local→remote is instant. If Proton ships an
  `events` subcommand (the SDK already has `subscribeToDriveEvents`), this can
  be upgraded to near-real-time both ways.
- Syncs `/my-files` only (not `photos`, `shared-with-me`, etc.) by default;
  point `PDRIVE_REMOTE_ROOT` elsewhere if you want a subtree.
