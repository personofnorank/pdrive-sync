"""The reconciler — rclone-bisync-style 3-way sync.

For each path we compare three states:
  L = current local filesystem
  R = current remote (Proton Drive) tree
  S = last-synced snapshot (SQLite)

and derive an action. Conflict policy (rclone `--conflict-loser pathname`):
keep both — the loser is renamed with a timestamped suffix.
"""
from __future__ import annotations

import shutil
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from . import config, proton, local
from .local import LocalNode, abs_of, sha1_file, is_excluded
from .proton import RemoteNode
from .state import StateDB
from .logutil import get_logger, notify

log = get_logger()


class Action(str, Enum):
    NONE = "none"
    UPLOAD = "upload"              # local -> remote (new or changed)
    DOWNLOAD = "download"          # remote -> local (new or changed)
    DELETE_LOCAL = "delete_local"
    DELETE_REMOTE = "delete_remote"
    MKDIR_LOCAL = "mkdir_local"
    MKDIR_REMOTE = "mkdir_remote"
    CONFLICT = "conflict"


@dataclass
class Change:
    path: str
    action: Action
    is_dir: bool
    local: Optional[LocalNode] = None
    remote: Optional[RemoteNode] = None
    reason: str = ""


def _local_changed(l: LocalNode, s, db: StateDB) -> bool:
    """Compare current local vs snapshot."""
    if s is None:
        return True
    if l.is_dir != bool(s["is_dir"]):
        return True
    if l.is_dir:
        return False
    # fast path: size + mtime
    if l.size == s["local_size"] and l.mtime is not None and s["local_mtime"] is not None \
            and abs(l.mtime - s["local_mtime"]) < 1e-6:
        return False
    # slow path: mtime shifted but content may be same (rclone/DS idea)
    if l.size is not None and l.size == s["local_size"] and s["local_sha1"]:
        cur = sha1_file(_resolve_local(db, l.path), config.HASH_COMPARE_LIMIT)
        if cur and cur == s["local_sha1"]:
            return False
    return True


def _remote_changed(r: RemoteNode, s) -> bool:
    if s is None:
        return True
    if r.is_dir != bool(s["is_dir"]):
        return True
    if r.is_dir:
        # directories: use mtime only as a hint; snapshot may predate children
        return False
    if r.uid and s["remote_uid"] and r.uid == s["remote_uid"]:
        # same node — check revision via sha1 or mtime
        if r.sha1 and s["remote_sha1"]:
            return r.sha1 != s["remote_sha1"]
        if r.mtime is not None and s["remote_mtime"] is not None:
            return abs(r.mtime - s["remote_mtime"]) > 1.0
        return False
    # UID differs from snapshot (replaced). Before declaring "changed", check
    # content: if the claimed SHA1 matches the snapshot, it's the same bytes
    # under a new node id — no transfer needed, just refresh the UID.
    if r.sha1 and s["remote_sha1"] and r.sha1.lower() == s["remote_sha1"].lower():
        return False
    return True


def _same_file_content(l: LocalNode, r: RemoteNode, db: StateDB) -> bool:
    """True when local file content provably matches the remote revision.

    Uses the remote's claimed SHA1 when available; falls back to size-only
    equality. Cheap insurance against false conflicts from mtime drift.
    """
    if l.is_dir or r.is_dir:
        return False
    if r.sha1 and l.size is not None:
        lsha = sha1_file(_resolve_local(db, l.path), config.HASH_COMPARE_LIMIT)
        if lsha:
            return lsha.lower() == r.sha1.lower()
    # fall back to size equality (weaker, but avoids a spurious conflict)
    return l.size is not None and r.size is not None and l.size == r.size


def _snapshot_identical(l: LocalNode, r: RemoteNode, db: StateDB) -> None:
    """Record a snapshot row for a path whose two sides already hold the same bytes.

    Needed when a path has no row at all — e.g. after a conflict copy has been
    renamed back into its canonical name. Without the row the daemon re-hashes
    that file on every single pass and never settles.
    """
    p = _resolve_local(db, l.path)
    try:
        st = p.stat()
        lsha = sha1_file(p, config.HASH_COMPARE_LIMIT)
    except OSError:
        return
    db.upsert(l.path, False,
              local_sha1=lsha,
              local_size=st.st_size,
              local_mtime=st.st_mtime,
              remote_uid=r.uid or None,
              remote_sha1=r.sha1,
              remote_mtime=r.mtime)


def _resolve_local(db: StateDB, path: str):
    """abs_of(path), but honouring the snapshot's sanitized local_path when the
    on-disk name differs from the remote/snapshot name."""
    row = db.get(path)
    if row is not None and "local_path" in row.keys() and row["local_path"]:
        return abs_of(row["local_path"])
    return abs_of(path)


def _remote_raw(c: "Change") -> str:
    """The RAW remote path to pass to the CLI for a change. The change's `path`
    is the sanitized canonical key; the remote node (when present) carries the
    raw path the CLI expects. Falls back to the key when there's no remote node
    (e.g. uploads, where the name is created from the local side)."""
    if c.remote is not None and c.remote.path:
        return c.remote.path
    return c.path


def compute_changes(local_tree: dict[str, LocalNode],
                    remote_tree: dict[str, RemoteNode],
                    db: StateDB,
                    force: bool = False,
                    scope: str = "",
                    trusted_local: bool = False) -> list[Change]:
    all_snapshot = db.all_paths()
    if scope:
        pfx = scope.rstrip("/") + "/"
        snapshot = {r["path"]: r for r in all_snapshot if r["path"] == scope or r["path"].startswith(pfx)}
    else:
        snapshot = {row["path"]: row for row in all_snapshot}

    # Map on-disk local path -> snapshot path, so a file the CLI wrote under a
    # sanitized name (e.g. 'Song: X' -> 'Song_ X') is recognised as the SAME
    # node as its remote/snapshot path, not a brand-new local file. Without
    # this the diff sees a phantom "new local" (upload) + "missing local"
    # (re-download) pair forever.
    local_to_snap = {}
    for row in all_snapshot:
        lp = row["local_path"] if "local_path" in row.keys() else None
        if lp and lp != row["path"]:
            local_to_snap[lp] = row["path"]

    # Re-key local_tree entries that correspond to a sanitized snapshot path so
    # they line up with remote/snapshot keys.
    if local_to_snap:
        remapped = {}
        for lp, node in local_tree.items():
            snap_path = local_to_snap.get(lp)
            if snap_path is not None:
                remapped[snap_path] = LocalNode(path=snap_path, is_dir=node.is_dir,
                                                size=node.size, mtime=node.mtime, sha1=node.sha1)
            else:
                remapped[lp] = node
        local_tree = remapped

    all_paths = set(local_tree) | set(remote_tree) | set(snapshot)
    changes: list[Change] = []

    for path in sorted(all_paths):
        l = local_tree.get(path)
        r = remote_tree.get(path)
        s = snapshot.get(path)
        # a path that exists on neither side is just snapshot cruft — drop it
        # (never "exclude" it, or we'd skip real deletions of ignored names).
        if not l and not r:
            if s is not None:
                db.delete(path)
            continue

        # --- CRITICAL SAFETY GUARD (untrusted local scan) -----------------
        # l is None here, but we can't tell "user deleted it" from "scan_local
        # skipped it" (unreadable/vanished mid-scan) or "resync ran before the
        # folder was locally visible". Deleting on that evidence wiped a 46G
        # folder. So: a missing-local path with a snapshot is NEVER deleted
        # from the remote unless the local scan is authoritative (a full,
        # unscoped daemon cycle). Even then, DIRECTORY deletes require the
        # parent to be visibly present in the scan (so a partially-failed scan
        # can't nuke a subtree). Scoped resync passes trusted_local=False.
        s_is_dir = bool(s["is_dir"]) if s is not None else False
        if s is not None and l is None and r is not None:
            # The path itself is missing locally. It's only safe to treat that
            # as a genuine local delete if the ENTIRE ancestor chain is present
            # in the scan — otherwise a partially-failed/unreadable scan of a
            # parent would cascade into deleting a whole subtree (the Music bug).
            def _ancestors_visible(p: str) -> bool:
                anc = p.rpartition("/")[0]
                while anc:
                    if anc not in local_tree:
                        return False
                    anc = anc.rpartition("/")[0]
                return True
            # Only FILES may be scan-deleted, and only when the full ancestor
            # chain is visible. DIRECTORIES are never deleted on scan-absence:
            # a missing dir in the scan is indistinguishable from an unreadable
            # one, and a wrong call cascades catastrophically (the Music bug).
            # Real dir deletes are handled by the watcher + the apply-time
            # re-stat in _delete_remote.
            # A non-NULL remote_uid proves the file exists remotely (sha1 can be
            # NULL if the CLI didn't return it during an earlier sync).
            has_remote_proof = (s["remote_sha1"] or s["remote_uid"]) if s else False
            safe_to_delete = trusted_local and (not s_is_dir) and _ancestors_visible(path) and has_remote_proof
            if safe_to_delete:
                changes.append(Change(path, Action.DELETE_REMOTE, s_is_dir, None, r, "deleted locally"))
            else:
                if s_is_dir:
                    changes.append(Change(path, Action.MKDIR_LOCAL, True, None, r,
                                          "dir missing locally — recreate (untrusted scan)"))
                else:
                    changes.append(Change(path, Action.DOWNLOAD, False, None, r,
                                          "file missing locally — re-download (untrusted scan)"))
            continue

        if is_excluded(path, is_dir=bool((l and l.is_dir) or (r and r.is_dir))):
            continue

        l_exists, r_exists, s_exists = l is not None, r is not None, s is not None
        l_changed = l_exists and _local_changed(l, s, db)
        r_changed = r_exists and _remote_changed(r, s)
        is_dir = (l and l.is_dir) or (r and r.is_dir) or s_is_dir

        if l_exists and r_exists:
            if not l_changed and not r_changed:
                continue
            if l_changed and not r_changed:
                changes.append(Change(path, Action.MKDIR_REMOTE if is_dir else Action.UPLOAD, is_dir, l, r, "local changed"))
            elif r_changed and not l_changed:
                changes.append(Change(path, Action.MKDIR_LOCAL if is_dir else Action.DOWNLOAD, is_dir, l, r, "remote changed"))
            else:
                # both flagged "changed". For files, verify with content hash
                # before declaring a conflict: an upload/download bumps the
                # parent's mtime, which would otherwise make every unchanged
                # sibling look remote-changed on the next cycle.
                if is_dir:
                    continue  # dirs: nothing to conflict about
                if _same_file_content(l, r, db):
                    # mtime drift only, or a stale row: content is identical, so
                    # bring the snapshot up to date. Recording it (rather than
                    # just continuing) matters because the stored remote_mtime
                    # semantics changed when the revision parser was fixed, and
                    # because a renamed-back conflict copy starts with no row at
                    # all — either way, without the refresh this file is re-hashed
                    # on every single pass and the tree never settles.
                    if l is not None and r is not None:
                        _snapshot_identical(l, r, db)
                    continue  # mtime drift only; content identical
                changes.append(Change(path, Action.CONFLICT, False, l, r, "both changed"))
        elif l_exists and not r_exists:
            if s_exists:
                # was synced, remote gone => remote deleted it
                changes.append(Change(path, Action.DELETE_LOCAL, is_dir, l, None, "deleted remotely"))
            else:
                # brand new local
                changes.append(Change(path, Action.MKDIR_REMOTE if is_dir else Action.UPLOAD, is_dir, l, None, "new local"))
        elif r_exists and not l_exists:
            if s_exists:
                changes.append(Change(path, Action.DELETE_REMOTE, is_dir, None, r, "deleted locally"))
            else:
                changes.append(Change(path, Action.MKDIR_LOCAL if is_dir else Action.DOWNLOAD, is_dir, None, r, "new remote"))
        elif s_exists and not l_exists and not r_exists:
            # gone from both: just drop the snapshot row
            db.delete(path)

    # order: mkdirs (parents first) -> transfers -> deletes (children first)
    def sort_key(c: Change):
        depth = c.path.count("/")
        rank = {Action.MKDIR_REMOTE: 0, Action.MKDIR_LOCAL: 0,
                Action.UPLOAD: 1, Action.DOWNLOAD: 1, Action.CONFLICT: 2,
                Action.DELETE_LOCAL: 3, Action.DELETE_REMOTE: 3}.get(c.action, 4)
        if c.action in (Action.DELETE_LOCAL, Action.DELETE_REMOTE):
            depth = -depth  # delete deepest first
        return (rank, depth)

    changes.sort(key=sort_key)

    # Conflict-storm brake. Conflict copies are excluded from sync, so nothing
    # ever cleans them up and a storm is unbounded disk growth: one pass on
    # 2026-08-20 wrote 71,717 copies (342 GiB) and filled the disk. Abort rather
    # than emit an unbounded number.
    conflicts = [c for c in changes if c.action == Action.CONFLICT]
    if conflicts and len(conflicts) > config.MAX_CONFLICTS and not force:
        log.error("ABORT: %d conflicts proposed in one pass; cap is MAX_CONFLICTS=%d. "
                  "Conflict copies are excluded from sync and nothing cleans them up. "
                  "No conflicts applied this pass; rerun with --force to proceed.",
                  len(conflicts), config.MAX_CONFLICTS)
        log.error("Blocked conflict paths (showing %d of %d):",
                  min(len(conflicts), 50), len(conflicts))
        for c in conflicts[:50]:
            log.error("  %-14s %s  (%s)", c.action.value, c.path, c.reason)
        notify("pdrive-sync: conflicts blocked",
               f"{len(conflicts)} conflicts proposed (cap {config.MAX_CONFLICTS}); see log",
               "critical")
        changes = [c for c in changes if c.action != Action.CONFLICT]

    deletes = [c for c in changes if c.action in (Action.DELETE_LOCAL, Action.DELETE_REMOTE)]

    # MAX_DELETE must count FILES affected, not top-level paths — a single
    # directory delete can destroy thousands of files (this is why the cap
    # failed to protect the 46G Music folder). Recurse to count real files.
    def _files_affected(c: Change) -> int:
        if not c.is_dir:
            return 1
        n = 0
        if c.action == Action.DELETE_LOCAL:
            p = abs_of(c.path)
            if p.is_dir():
                for _, _, files in os.walk(p):
                    n += len(files)
        else:  # DELETE_REMOTE — count from snapshot's known subtree
            n = max(1, len(db.children_of(c.path)))
        return max(1, n)

    total_delete_files = sum(_files_affected(c) for c in deletes)
    if deletes and (total_delete_files > config.MAX_DELETE and not force):
        log.error("ABORT: deletes would remove ~%d files across %d path(s); "
                  "cap is MAX_DELETE=%d. No deletes applied; rerun with --force to proceed.",
                  total_delete_files, len(deletes), config.MAX_DELETE)
        # Log exactly which paths are at stake so the user can judge legitimacy.
        log.error("Blocked delete paths (%d):", len(deletes))
        for c in deletes:
            log.error("  %-14s %s  (~%d files)  (%s)",
                      c.action.value, c.path, _files_affected(c), c.reason)
        notify("pdrive-sync: deletes blocked",
               f"~{total_delete_files} files would be deleted (cap {config.MAX_DELETE}); see log for paths", "critical")
        return [c for c in changes if c.action not in (Action.DELETE_LOCAL, Action.DELETE_REMOTE)]
    return changes


class Reconciler:
    def __init__(self, db: StateDB):
        self.db = db

    # -- individual ops ----------------------------------------------------
    def _mkdir_local(self, c: Change):
        abs_of(c.path).mkdir(parents=True, exist_ok=True)
        self.db.upsert(c.path, True, local_mtime=c.remote.mtime if c.remote else None,
                       remote_uid=c.remote.uid if c.remote else None,
                       remote_mtime=c.remote.mtime if c.remote else None)
        log.info("mkdir local  %s", c.path)

    def _mkdir_remote(self, c: Change):
        parent, _, name = c.path.rpartition("/")
        proton.create_folder(parent, name)
        # capture the new folder's remote identity so the snapshot matches and
        # children don't get re-synced as "changed" every cycle.
        uid, rmt = None, None
        r = proton.info(c.path)
        if r:
            uid = r.get("uid")
            folder = r.get("folder") or {}
            rmt = proton._parse_time(folder.get("claimedModificationTime")) \
                or proton._parse_time(r.get("modificationTime"))
        self.db.upsert(c.path, True,
                       local_mtime=c.local.mtime if c.local else None,
                       remote_uid=uid, remote_mtime=rmt)
        log.info("mkdir remote %s", c.path)

    def _upload(self, c: Change):
        parent, _, _ = c.path.rpartition("/")
        src = _resolve_local(self.db, c.path)
        proton.upload(str(src), parent, strategy="replace")
        # re-read remote state for snapshot accuracy
        r = proton.info(c.path)
        uid = r.get("uid") if r else None
        rev = proton.revision(r or {})
        rsha = (rev.get("claimedDigests") or {}).get("sha1")
        rmt = proton._parse_time(rev.get("claimedModificationTime")) if rev else None
        if rmt is None and r:
            rmt = proton._parse_time(r.get("modificationTime"))
        p = src
        st = p.stat()
        self.db.upsert(c.path, False,
                       local_sha1=sha1_file(p, config.HASH_COMPARE_LIMIT),
                       local_size=st.st_size, local_mtime=st.st_mtime,
                       remote_uid=uid, remote_sha1=rsha, remote_mtime=rmt)
        log.info("uploaded     %s (%d bytes)", c.path, st.st_size)

    def _download(self, c: Change):
        # The CLI sanitizes Windows-illegal chars (: ? " < > | \ and trailing
        # dots/spaces) to '_' in the LOCAL filename on download, so the file
        # lands at a different path than the remote name. Find the real file.
        target = abs_of(c.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        proton.download(_remote_raw(c), str(target.parent), strategy="replace")
        actual = target if target.exists() else self._find_downloaded(c)
        if actual is None:
            raise CliError(f"download produced no local file for {c.path}")
        st = actual.stat()
        lsha = sha1_file(actual, config.HASH_COMPARE_LIMIT)
        if c.remote and c.remote.sha1 and lsha and lsha != c.remote.sha1:
            log.warning("SHA1 mismatch after download: %s (local=%s remote=%s)", c.path, lsha, c.remote.sha1)
        from .local import rel_of
        actual_rel = rel_of(actual)
        self.db.upsert(c.path, False,
                       local_sha1=lsha, local_size=st.st_size, local_mtime=st.st_mtime,
                       remote_uid=c.remote.uid if c.remote else None,
                       remote_sha1=c.remote.sha1 if c.remote else None,
                       remote_mtime=c.remote.mtime if c.remote else None,
                       local_path=actual_rel if actual_rel != c.path else None)
        if actual != target:
            log.info("downloaded   %s (%d bytes)  [sanitized to %s]", c.path, st.st_size, actual.name)
        else:
            log.info("downloaded   %s (%d bytes)", c.path, st.st_size)

    @staticmethod
    def _sanitize_local_name(name: str) -> str:
        # Mirror the CLI's download sanitization. Verified empirically against
        # the official CLI: these chars become '_' in the local filename.
        # Trailing dots/spaces are NOT changed by the CLI (confirmed by probe).
        import re
        return re.sub(r'[:?"<>|*\\]', '_', name)

    def _find_downloaded(self, c: Change):
        """Locate the file the CLI actually wrote after a download, accounting
        for name sanitization. Returns a Path or None."""
        from pathlib import Path
        parent = abs_of(c.path).parent
        want = self._sanitize_local_name(c.path.rsplit("/", 1)[-1])
        cand = parent / want
        if cand.exists():
            return cand
        # last resort: newest file in the parent dir
        try:
            files = [p for p in parent.iterdir() if p.is_file()]
            if files:
                return max(files, key=lambda p: p.stat().st_mtime)
        except OSError:
            pass
        return None

    def _delete_local(self, c: Change):
        p = _resolve_local(self.db, c.path)
        if c.is_dir:
            shutil.rmtree(p, ignore_errors=True)
            self.db.delete_under(c.path)
        else:
            p.unlink(missing_ok=True)
            self.db.delete(c.path)
        log.info("deleted local  %s", c.path)

    def _delete_remote(self, c: Change):
        # Belt-and-braces: never trash a remote dir/file whose local path
        # actually still exists on disk at apply time (scan was wrong).
        p = _resolve_local(self.db, c.path)
        if p.exists():
            log.warning("DELETE_REMOTE %s aborted: local path still exists on disk", c.path)
            # it's still here — treat as in-sync, just refresh snapshot
            return
        proton.trash(_remote_raw(c))
        self.db.delete_under(c.path) if c.is_dir else self.db.delete(c.path)
        log.info("trashed remote %s", c.path)

    def _conflict(self, c: Change):
        """Keep both. Newer mtime wins the canonical name (rclone 'newer' resolve).

        Order matters: the remote copy is FETCHED FIRST into a scratch directory,
        and only then are names shuffled locally. The old order moved the local
        file aside and *then* downloaded, so a failed download left the move
        standing: 31,979 conflict copies ended up with no canonical file beside
        them, and Sorted/2013/2013_05/2013_05_29 held 99 conflicts and zero
        canonicals while the remote was still intact. Fetching first means a
        failed or partial transfer cannot lose either side.
        """
        p = _resolve_local(self.db, c.path)
        local_mtime = c.local.mtime or 0
        remote_mtime = c.remote.mtime or 0
        stamp = datetime.now().strftime(config.CONFLICT_SUFFIX_FMT)
        suffix_path = p.with_name(p.name.replace(p.suffix, "") + stamp + p.suffix) if p.suffix else \
            p.with_name(p.name + stamp)

        # 1. Fetch the remote version into scratch space, which is excluded from
        #    sync (.pdrive-sync-tmp*). Nothing local is touched yet.
        tmp_dir = p.parent / (config.CONFLICT_TMP_PREFIX + stamp)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            proton.download(_remote_raw(c), str(tmp_dir), strategy="keep-both")
            fetched = sorted(q for q in tmp_dir.iterdir() if q.is_file())
            if not fetched:
                raise RuntimeError(f"conflict fetch produced no file for {c.path}")

            # 2. Bytes are on disk — now the local renames, which cannot fail
            #    for want of network access.
            if local_mtime >= remote_mtime:
                # local wins name; the remote copy lands beside it
                os.replace(str(fetched[0]), str(suffix_path))
                log.info("conflict %s: local kept, remote saved as %s", c.path, suffix_path.name)
            else:
                # remote wins the canonical name; the local copy is preserved as
                # the conflict copy
                os.replace(str(p), str(suffix_path))
                os.replace(str(fetched[0]), str(p))
                log.info("conflict %s: remote kept, local saved as %s", c.path, suffix_path.name)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        notify("pdrive-sync: conflict", f"{c.path} — both versions kept", "normal")
        # refresh snapshot to the winning side; the .conflict-* copy is excluded
        self._refresh_snapshot(c.path)

    def _refresh_snapshot(self, path: str):
        p = _resolve_local(self.db, path)
        if not p.exists():
            self.db.delete(path)
            return
        st = p.stat()
        r = proton.info(path)
        rev = proton.revision(r or {})
        self.db.upsert(path, p.is_dir(),
                       local_sha1=None if p.is_dir() else sha1_file(p, config.HASH_COMPARE_LIMIT),
                       local_size=None if p.is_dir() else st.st_size,
                       local_mtime=st.st_mtime,
                       remote_uid=(r or {}).get("uid"),
                       remote_sha1=(rev.get("claimedDigests") or {}).get("sha1"),
                       remote_mtime=proton._parse_time(rev.get("claimedModificationTime")) if rev else None)

    # -- main entry ----------------------------------------------------------
    def apply(self, changes: list[Change], dry_run: bool = False,
              no_delete: bool = False) -> tuple[int, int]:
        if no_delete:
            skipped = [c for c in changes if c.action in (Action.DELETE_LOCAL, Action.DELETE_REMOTE)]
            if skipped:
                for c in skipped:
                    log.info("[no-delete] suppressed %s %s", c.action.value, c.path)
                changes = [c for c in changes if c.action not in (Action.DELETE_LOCAL, Action.DELETE_REMOTE)]
        if dry_run:
            # No side effects: no uploads, downloads, mkdirs, deletes, DB writes.
            for c in changes:
                log.info("[dry-run] %-14s %s  (%s)", c.action.value, c.path, c.reason)
            return len(changes), 0
        ok, failed = 0, 0
        for c in changes:
            try:
                {Action.MKDIR_LOCAL: self._mkdir_local,
                 Action.MKDIR_REMOTE: self._mkdir_remote,
                 Action.UPLOAD: self._upload,
                 Action.DOWNLOAD: self._download,
                 Action.DELETE_LOCAL: self._delete_local,
                 Action.DELETE_REMOTE: self._delete_remote,
                 Action.CONFLICT: self._conflict}[c.action](c)
                ok += 1
            except Exception as e:  # resilient: log, continue with next change
                failed += 1
                log.error("FAILED %s %s: %s", c.action.value, c.path, e)
        return ok, failed

    def sync_once(self, force: bool = False, deep: bool = True, scope: str = "",
                  dry_run: bool = False, no_delete: bool = False) -> tuple[int, int]:
        """One reconcile cycle, optionally limited to a subtree (`scope`).

        deep=True  -> full recursive remote walk (accurate, slow).
        deep=False -> pruned walk: reuse snapshot for folders whose remote
                      mtime is unchanged (fast; may miss deep edits whose
                      parent mtimes didn't change — the periodic deep cycle
                      catches those).
        scope      -> POSIX relative path; when set, only that subtree is
                      scanned/diffed/applied. Used by the resumable resync so
                      progress commits one top-level folder at a time.
        dry_run    -> compute and log changes but apply nothing (no uploads,
                      downloads, deletes, or DB writes).
        """
        local_tree = local.scan_local()
        remote_tree = proton.walk_remote(scope, db=self.db, prune=not deep)
        if scope:
            pfx = scope.rstrip("/") + "/"
            local_tree = {p: n for p, n in local_tree.items() if p == scope or p.startswith(pfx)}
            remote_tree = {p: n for p, n in remote_tree.items() if p == scope or p.startswith(pfx)}

        # --- Remote-implosion guards --------------------------------------
        # (a) whole-tree: a nearly-empty remote read when the snapshot is large
        #     means the walk failed (auth/network), not a mass deletion.
        # (b) per-folder: if a specific top-level folder's returned subtree is
        #     drastically smaller than its tracked subtree while the walk claims
        #     to have covered that folder, that folder's diff is untrusted — drop
        #     its deletes. This is what nearly wiped 5,627 Pictures files when
        #     the walk dropped just that subtree.
        tracked = self.db.count()
        if not scope and tracked > 100 and len(remote_tree) < max(10, tracked // 20):
            log.error("remote walk returned only %d nodes but snapshot tracks %d — "
                      "walk likely failed (auth/network). Skipping cycle.", len(remote_tree), tracked)
            notify("pdrive-sync: skipped cycle",
                   f"remote read looked empty ({len(remote_tree)} nodes vs {tracked} tracked) — likely auth/network blip", "critical")
            return 0, 0

        # per-folder partial-implosion check (unscoped cycles only)
        untrusted_folders: set[str] = set()
        if not scope:
            import collections
            tracked_by_root = collections.Counter()
            for r in self.db.all_paths():
                root0 = r["path"].split("/")[0] if r["path"] else ""
                tracked_by_root[root0] += 1
            returned_by_root = collections.Counter(
                (p.split("/")[0] if p else "") for p in remote_tree.keys())
            for root0, tcount in tracked_by_root.items():
                if not root0:
                    continue
                # only judge folders the walk was expected to return (skip tiny ones)
                if tcount > 50:
                    rcount = returned_by_root.get(root0, 0)
                    # if we got back <20% of what we track for this folder, the
                    # walk dropped the subtree -> untrusted
                    if rcount < max(2, tcount // 5):
                        untrusted_folders.add(root0)
                        log.warning("remote implosion in folder '%s': returned %d nodes but snapshot "
                                    "tracks %d — its deletes are untrusted and will be dropped",
                                    root0, rcount, tcount)
        if untrusted_folders:
            notify("pdrive-sync: dropped phantom deletes",
                   "remote walk dropped folder(s): " + ", ".join(sorted(untrusted_folders)), "normal")

        # The local scan is authoritative (trusted) only for a full, unscoped
        # cycle. A scoped resync filters the scan, so a "missing locally" there
        # is untrusted and must never trigger a remote delete.
        changes = compute_changes(local_tree, remote_tree, self.db, force=force,
                                  scope=scope, trusted_local=(not scope))
        # strip deletes belonging to untrusted (imploded) folders
        if untrusted_folders:
            changes = [c for c in changes
                       if not (c.action in (Action.DELETE_LOCAL, Action.DELETE_REMOTE)
                               and c.path.split("/")[0] in untrusted_folders)]

        # Snapshot in-sync paths too: a path that's identical on both sides
        # produces NO change, so without this it would never be recorded and the
        # snapshot would stay permanently incomplete (the resync stall).
        if not dry_run:
            self._snapshot_insync(local_tree, remote_tree, scope)

        if not changes:
            log.debug("sync: in sync (%d local, %d remote, %d tracked)",
                      len(local_tree), len(remote_tree), self.db.count())
            return 0, 0
        log.info("sync: %d change(s)%s%s", len(changes),
                 " [dry-run]" if dry_run else "", " [no-delete]" if no_delete else "")
        return self.apply(changes, dry_run=dry_run, no_delete=no_delete)

    def apply_toplevel_remote(self, no_delete: bool = False) -> int:
        """Cheap remote sync of the TOP level only (one root listing). Applies
        remote-side creates and deletes of top-level entries immediately, so a
        folder added/removed in the Drive web UI lands within a poll cycle
        instead of waiting for the slow recursive deep walk. Returns count."""
        local_root = {p: n for p, n in local.scan_local().items() if "/" not in p}
        remote_root = {n.key: n for n in proton.list_remote("")}
        snapshot = {r["path"]: r for r in self.db.all_paths() if "/" not in r["path"]}
        changes = []
        for path in set(local_root) | set(remote_root) | set(snapshot):
            l = local_root.get(path)
            r = remote_root.get(path)
            s = snapshot.get(path)
            if l and not r and s is not None:
                # remote deleted a top-level entry -> delete local
                changes.append(Change(path, Action.DELETE_LOCAL, l.is_dir, l, None, "remote deleted (top-level)"))
            elif r and not l and s is None:
                # brand new remote top-level entry -> create locally
                changes.append(Change(path, Action.MKDIR_LOCAL if r.is_dir else Action.DOWNLOAD,
                                      r.is_dir, None, r, "new remote (top-level)"))
        if not changes:
            return 0
        ok, _ = self.apply(changes, no_delete=no_delete)
        return ok

    def _snapshot_insync(self, local_tree, remote_tree, scope: str = "") -> int:
        """Record snapshot rows for paths present and matching on BOTH sides but
        not yet tracked. This is what makes the baseline converge: identical
        content gets a snapshot row instead of being re-diffed forever."""
        changed = {c.path for c in []}  # placeholder; real set passed via diff
        n = 0
        snapshot = {r["path"]: r for r in self.db.all_paths()}
        for path, r in remote_tree.items():
            if path in snapshot:
                continue  # already tracked
            l = local_tree.get(path)
            if l is None:
                continue  # remote-only; will be handled as a download
            if l.is_dir != r.is_dir:
                continue
            if l.is_dir:
                # dir present both sides, untracked -> record it
                self.db.upsert(path, True, local_mtime=l.mtime,
                               remote_uid=r.uid, remote_mtime=r.mtime)
                n += 1
            else:
                # file: only snapshot when content provably matches
                if r.sha1 and l.size is not None and l.size == r.size:
                    lsha = sha1_file(_resolve_local(self.db, path), config.HASH_COMPARE_LIMIT)
                    if lsha and lsha.lower() == r.sha1.lower():
                        self.db.upsert(path, False, local_sha1=lsha, local_size=l.size,
                                       local_mtime=l.mtime, remote_uid=r.uid,
                                       remote_sha1=r.sha1, remote_mtime=r.mtime)
                        n += 1
        if n:
            log.info("snapshot: recorded %d in-sync path(s)", n)
        return n
