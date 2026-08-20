"""Daemon: watchdog observer (instant local pickup) + remote poll loop."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import config, proton
from .local import rel_of, is_excluded
from .logutil import get_logger, notify
from .reconcile import Reconciler
from .state import StateDB

log = get_logger()

# Newest CLI version this build's flag-mapping has been verified against.
# Bump this after testing a newer CLI. If the installed CLI is newer, we warn
# once at startup (flags may have changed) instead of failing every transfer.
_TESTED_CLI = (0, 8, 0)


def _check_cli_compat() -> None:
    """Warn if the installed CLI version is unrecognised or newer than tested.

    Returns normally either way — this is an early-warning check, not a hard
    gate. The dialect mapping in proton.py handles 0.6.x/0.7.x/0.8.x; anything
    newer just gets a heads-up notification so a breaking flag change is caught
    at startup rather than as a storm of mid-cycle transfer failures.
    """
    import re
    import subprocess
    try:
        proc = subprocess.run([config.CLI, "version"], capture_output=True, text=True, timeout=30)
        m = re.search(r"cli-drive@(\d+)\.(\d+)\.(\d+)", proc.stdout + proc.stderr)
        if not m:
            log.warning("could not parse CLI version from %r", config.CLI)
            notify("pdrive-sync: CLI version unknown",
                   "couldn't parse proton-drive version; sync may misbehave", "normal")
            return
        ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        log.info("proton-drive CLI version: %d.%d.%d (tested up to %d.%d.%d)",
                 *ver, *_TESTED_CLI)
        if ver > _TESTED_CLI:
            log.warning("CLI %s is newer than this build was tested against (%s); "
                        "conflict flags may have changed",
                        ".".join(map(str, ver)), ".".join(map(str, _TESTED_CLI)))
            notify("pdrive-sync: untested CLI version",
                   f"CLI {'.'.join(map(str,ver))} > tested {'.'.join(map(str,_TESTED_CLI))}; watch for transfer errors", "normal")
    except Exception as e:
        log.warning("CLI version check failed: %s", e)


class _Lock:
    """Single-instance lock via flock."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def acquire(self) -> bool:
        import fcntl

        config.ensure_dirs()
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.fd.write(str(os.getpid()))
        self.fd.flush()
        return True

    def release(self):
        if self.fd:
            self.fd.close()


class Watcher:
    def __init__(self, on_change):
        self.on_change = on_change
        self.observer = None

    def start(self):
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        handler = _Handler(self.on_change)
        self.observer = Observer()
        self.observer.schedule(handler, str(config.SYNC_ROOT), recursive=True)
        self.observer.start()
        log.info("watching %s", config.SYNC_ROOT)

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)


class _Handler(__import__("watchdog.events", fromlist=["FileSystemEventHandler"]).FileSystemEventHandler):
    def __init__(self, on_change):
        self.on_change = on_change

    def _emit(self, path):
        try:
            rel = rel_of(path)
        except ValueError:
            return
        if not rel or is_excluded(rel):
            return
        self.on_change(rel)

    def on_created(self, e):
        self._emit(e.src_path)

    def on_modified(self, e):
        self._emit(e.src_path)

    def on_deleted(self, e):
        self._emit(e.src_path)

    def on_moved(self, e):
        self._emit(e.src_path)
        self._emit(e.dest_path)


def run_daemon(force: bool = False, no_delete: bool = False):
    import os as _os
    if not no_delete and _os.environ.get("PDRIVE_NO_DELETE", "0") == "1":
        no_delete = True
    config.ensure_dirs()
    lock = _Lock(config.LOCK_PATH)
    if not lock.acquire():
        log.error("another pdrive-sync instance is running (lock: %s)", config.LOCK_PATH)
        sys.exit(1)

    log.info("pdrive-sync starting: %s <-> %s%s", config.SYNC_ROOT, config.REMOTE_ROOT,
             "  [NO-DELETE mode]" if no_delete else "")

    # CLI compatibility self-check: the conflict-flag dialect changed at 0.8.0
    # (`-c` removed, split into `-f`/`-d` with renamed values). If the installed
    # CLI is unrecognised or newer than the newest version this build was tested
    # against, warn up front rather than failing every transfer mid-cycle.
    _check_cli_compat()

    # The session lives in the OS keyring, which may still be locked right after
    # a reboot/login when a user service starts. Retry the auth check for a
    # couple of minutes so a slow keyring unlock doesn't force a re-login.
    authed = False
    for attempt in range(24):  # up to ~2 min
        if proton.check_auth():
            authed = True
            break
        time.sleep(5)
    if not authed:
        log.error("Proton Drive CLI is not authenticated (or keyring still locked; binary at %s).", config.CLI)
        log.error("If this persists across reboot, run: %s auth login", config.CLI)
        notify("pdrive-sync: not authenticated", "Run proton-drive auth login (or unlock keyring)", "critical")
        sys.exit(2)

    # one instance per sync root; the events lock in the CLI means concurrent
    # CLI invocations are fine, but two daemons on one root would fight.
    db = StateDB()
    reconciler = Reconciler(db)

    # initial baseline if DB is empty and trees are non-empty
    if db.count() == 0:
        log.info("no state found — establishing baseline (resync)")
        _initial_resync(reconciler, force=force)

    stop = threading.Event()
    dirty = threading.Event()
    dirty.set()  # run one full cycle at startup

    def on_local_change(rel: str):
        log.debug("local change: %s", rel)
        dirty.set()

    watcher = Watcher(on_local_change)
    watcher.start()

    def shutdown(*_):
        log.info("shutting down")
        stop.set()
        dirty.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    last_cycle = 0.0
    last_deep = 0.0  # force a deep cycle on first pass
    try:
        while not stop.is_set():
            woke = dirty.wait(timeout=config.POLL_INTERVAL)
            if stop.is_set():
                break
            now = time.time()
            if woke and now - last_cycle < config.DEBOUNCE_SECONDS:
                # debounce bursty local events
                time.sleep(config.DEBOUNCE_SECONDS - (now - last_cycle))
            dirty.clear()
            last_cycle = time.time()

            # Adaptive remote detection: cheap root fingerprint every cycle;
            # if it changed (or DEEP_INTERVAL elapsed) do a full walk,
            # otherwise do the fast pruned walk.
            deep = (now - last_deep) >= config.DEEP_INTERVAL
            try:
                fp = proton.root_fingerprint()
                if fp != db.get_meta("root_fingerprint"):
                    db.set_meta("root_fingerprint", fp)
                    deep = True
                    log.debug("root fingerprint changed -> deep cycle")
                    # Apply top-level remote creates/deletes immediately — the
                    # root listing is one cheap call, so a folder removed/added
                    # in the Drive web UI shouldn't wait ~an hour for the full
                    # walk to reach it.
                    try:
                        applied_top = reconciler.apply_toplevel_remote()
                        if applied_top:
                            log.info("top-level remote changes applied: %d", applied_top)
                    except Exception as e:
                        log.warning("top-level remote apply failed: %s", e)
            except Exception as e:
                log.warning("root fingerprint failed: %s", e)

            try:
                ok, failed = reconciler.sync_once(force=force, deep=deep, no_delete=no_delete)
                if deep:
                    last_deep = now
                if ok or failed:
                    log.info("cycle done (%s): %d applied, %d failed",
                             "deep" if deep else "pruned", ok, failed)
                    if failed:
                        notify("pdrive-sync: errors", f"{failed} operation(s) failed; see log", "normal")
            except Exception as e:
                log.exception("sync cycle error: %s", e)
    finally:
        watcher.stop()
        db.close()
        lock.release()
        log.info("stopped")


def _initial_resync(reconciler: Reconciler, force: bool = False):
    """Baseline: bring both trees together. Union strategy — everything that
    exists on either side ends up on both, conflicts keep both copies."""
    log.info("resync: scanning local and remote trees")
    ok, failed = reconciler.sync_once(force=force)
    log.info("resync complete: %d applied, %d failed", ok, failed)
    if ok or failed:
        notify("pdrive-sync: baseline done", f"{ok} items synced, {failed} failed")
