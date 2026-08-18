"""`pdrive-sync` command-line interface."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, proton
from .logutil import get_logger
from .state import StateDB

log = get_logger()
UNIT = "pdrive-sync.service"


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user"] + list(args), capture_output=True, text=True)


def cmd_start(args):
    r = _systemctl("start", UNIT)
    if r.returncode != 0:
        print(r.stderr.strip() or "failed to start; is the unit installed? (pdrive-sync install)")
        return 1
    print("started. logs: pdrive-sync logs")


def cmd_stop(args):
    _systemctl("stop", UNIT)
    print("stopped")


def cmd_status(args):
    r = _systemctl("status", UNIT, "--no-pager", "-l")
    print(r.stdout[:3000] if r.stdout else r.stderr)
    db = StateDB()
    print(f"\ntracked paths: {db.count()}   sync root: {config.SYNC_ROOT}   remote: {config.REMOTE_ROOT}")
    db.close()


def cmd_logs(args):
    n = args.lines
    try:
        lines = config.LOG_PATH.read_text().splitlines()[-n:]
        print("\n".join(lines))
    except FileNotFoundError:
        print("no log yet")


def cmd_run(args):
    from .daemon import run_daemon
    run_daemon(force=args.force, no_delete=args.no_delete)


def cmd_sync(args):
    """One-shot sync cycle (foreground), then exit."""
    from .reconcile import Reconciler
    db = StateDB()
    ok, failed = Reconciler(db).sync_once(force=args.force, dry_run=args.dry_run,
                                         no_delete=args.no_delete)
    print(("sync plan: %d change(s), nothing applied (dry-run)" if args.dry_run
           else "sync complete: %d applied, %d failed") % (ok, failed))
    db.close()
    return 1 if failed else 0


def cmd_plan(args):
    """Dry-run across the whole tree: show what WOULD change, apply nothing."""
    from .reconcile import Reconciler
    db = StateDB()
    ok, failed = Reconciler(db).sync_once(force=False, deep=True, dry_run=True)
    print(f"plan: {ok} change(s) would be applied; nothing was touched")
    db.close()
    return 0


def cmd_resync(args):
    """Rebuild the baseline, one top-level folder at a time (resumable).

    Re-running after an interruption skips folders already in the snapshot, so
    a multi-GB first sync can be stopped and resumed freely. Progress is written
    to the log and to a `resync_progress` meta key (see `resync-status`).
    """
    from .reconcile import Reconciler
    from . import proton, local
    from .logutil import notify
    db = StateDB()
    rec = Reconciler(db)

    # top-level entries = union of local + remote root children (dirs first)
    top = {}
    for p, n in local.scan_local().items():
        if "/" not in p:
            top[p] = n.is_dir
    try:
        for n in proton.list_remote(""):
            top.setdefault(n.path, n.is_dir)
    except Exception as e:
        log.warning("could not list remote root (%s); using local only", e)

    total_ok = total_fail = 0
    names = sorted(top, key=lambda p: (not top[p], p))  # dirs first
    dry = getattr(args, "dry_run", False)
    nodel = getattr(args, "no_delete", False)
    log.info("resync: %d top-level entries: %s%s%s", len(names), ", ".join(names),
             "  [dry-run — nothing applied]" if dry else "",
             "  [no-delete]" if nodel else "")
    db.set_meta("resync_total", str(len(names)))
    db.set_meta("resync_done", "0")
    db.set_meta("resync_state", "running")
    for i, name in enumerate(names, 1):
        # A folder counts as baselined only if its subtree is FULLY captured.
        # Comparing children_of()>0 is wrong: partial earlier runs leave a few
        # rows, so a folder 1% baselined would be skipped. Instead compare the
        # snapshot's file count under the folder against the actual local file
        # count (excluding ignored dirs); if they differ materially, re-sync.
        if top.get(name):  # it's a folder
            snap_files = sum(1 for r in db.children_of(name) if not r["is_dir"])
            local_files = sum(1 for p, n in local.scan_local().items()
                              if p.startswith(name.rstrip("/") + "/") and not n.is_dir)
            # done when snapshot covers essentially all local files (some may be
            # remote-only or excluded; allow small slack)
            done = db.get(name) is not None and local_files > 0 and snap_files >= local_files - 2
            if not done and db.get(name) is not None:
                log.info("resync [%d/%d] %s: snapshot has %d/%d local files -> re-sync",
                         i, len(names), name, snap_files, local_files)
        else:
            done = db.get(name) is not None
        if done:
            log.info("resync [%d/%d] SKIP %s (already baselined)", i, len(names), name)
            db.set_meta("resync_done", str(i))
            continue
        if db.get(name) is not None:
            log.info("resync [%d/%d] RE-SYNC %s", i, len(names), name)
        else:
            log.info("resync [%d/%d] SYNC %s ...", i, len(names), name)
        db.set_meta("resync_current", name)
        ok, failed = rec.sync_once(force=True, deep=True, scope=name, dry_run=dry, no_delete=nodel)
        total_ok += ok; total_fail += failed
        db.set_meta("resync_done", str(i))
        log.info("resync [%d/%d] DONE %s: %d applied, %d failed (tracked=%d)",
                 i, len(names), name, ok, failed, db.count())

    db.set_meta("resync_state", "complete")
    db.set_meta("resync_current", "")
    log.info("RESYNC COMPLETE%s: %d applied, %d failed (%d paths tracked)",
             " [dry-run]" if dry else "", total_ok, total_fail, db.count())
    if not dry:
        notify("pdrive-sync: baseline complete",
               f"{db.count()} paths synced, {total_fail} failed")
    db.close()
    return 1 if total_fail else 0


def cmd_resync_status(args):
    """Show baseline (resync) progress at a glance."""
    db = StateDB()
    state = db.get_meta("resync_state") or "not started"
    done = db.get_meta("resync_done") or "0"
    total = db.get_meta("resync_total") or "?"
    current = db.get_meta("resync_current") or "-"
    tracked = db.count()
    print(f"state:    {state}")
    print(f"progress: {done}/{total} top-level folders")
    if state == "running":
        print(f"current:  {current}")
    print(f"tracked:  {tracked} paths")
    db.close()


def cmd_conflicts(args):
    """List conflict copies currently in the local tree."""
    root = config.SYNC_ROOT
    found = [p for p in root.rglob("*") if ".conflict-" in p.name]
    if not found:
        print("no conflicts 🎉")
        return 0
    for p in sorted(found):
        print(p.relative_to(root))
    return 0


def cmd_install(args):
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    python = config.APP_DIR / ".venv" / "bin" / "python"
    env_file = config.APP_DIR / "pdrive-sync.env"
    if not env_file.exists():
        env_file.write_text(
            "# pdrive-sync daemon environment. Set PDRIVE_NO_DELETE=1 to run the\n"
            "# service in no-delete mode (apply everything except deletes).\n"
            "PDRIVE_NO_DELETE=1\n"
        )
    unit = f"""[Unit]
Description=Proton Drive bidirectional sync (pdrive-sync)
# Start only once the graphical session is up: the Proton session lives in the
# OS keyring, which PAM unlocks at graphical login. Starting earlier (bare
# default.target) meant the keyring was still locked -> looked logged-out.
After=graphical-session.target network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} -m pdrive_sync.cli run
WorkingDirectory={config.APP_DIR}
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-{env_file}

[Install]
WantedBy=graphical-session.target
"""
    (unit_dir / UNIT).write_text(unit)
    _systemctl("daemon-reload")
    _systemctl("enable", UNIT)
    print(f"installed {unit_dir / UNIT} (enabled)")
    print(f"daemon mode env: {env_file}  (PDRIVE_NO_DELETE=1 currently)")
    print("start with: pdrive-sync start")


def cmd_uninstall(args):
    _systemctl("disable", "--now", UNIT)
    unit = Path.home() / ".config" / "systemd" / "user" / UNIT
    unit.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    print("uninstalled")


def main(argv=None):
    config.ensure_dirs()
    p = argparse.ArgumentParser(prog="pdrive-sync", description="Bidirectional Proton Drive sync for Linux")
    p.add_argument("--force", action="store_true", help="bypass the MAX_DELETE safety cap")
    p.add_argument("--dry-run", action="store_true", help="show what would change; apply nothing")
    p.add_argument("--no-delete", action="store_true", help="apply everything except deletes (extra-safe first run)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start", help="start the background service")
    sub.add_parser("stop", help="stop the background service")
    sub.add_parser("status", help="service + sync status")
    sp = sub.add_parser("logs", help="tail the log"); sp.add_argument("-n", "--lines", type=int, default=50)
    sp = sub.add_parser("run", help="run the daemon in the foreground")
    sp = sub.add_parser("sync", help="one-shot sync cycle, then exit (use --dry-run to preview)")
    sp = sub.add_parser("plan", help="dry-run: show what WOULD change, apply nothing")
    sp = sub.add_parser("resync", help="rebuild baseline from scratch (use --dry-run to preview)"); sp.add_argument("-y", "--yes", action="store_true")
    sub.add_parser("resync-status", help="show baseline (resync) progress")
    sub.add_parser("conflicts", help="list conflict copies")
    sub.add_parser("install", help="install & enable the systemd user service")
    sub.add_parser("uninstall", help="disable & remove the systemd user service")

    args = p.parse_args(argv)
    fn = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "logs": cmd_logs,
          "run": cmd_run, "sync": cmd_sync, "plan": cmd_plan,
          "resync": cmd_resync, "resync-status": cmd_resync_status,
          "conflicts": cmd_conflicts,
          "install": cmd_install, "uninstall": cmd_uninstall}[args.cmd]
    return fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
