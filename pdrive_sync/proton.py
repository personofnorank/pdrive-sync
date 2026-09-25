"""Wrapper around the official `proton-drive` CLI.

Every remote operation goes through the CLI with `-j` (JSON) where listing is
involved, and parses the human summary for transfers. All functions are
synchronous; the daemon runs them in a worker thread.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional
from . import config
from .logutil import get_logger
from . import local as _local_mod

log = get_logger()


class CliError(RuntimeError):
    pass


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [config.CLI] + args
    log.debug("CLI: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CliError(f"timeout running {' '.join(cmd)}") from e


@dataclass
class RemoteNode:
    path: str          # RAW remote path (used as CLI argument), relative to REMOTE_ROOT
    name: str          # raw name
    is_dir: bool
    uid: str = ""
    size: Optional[int] = None
    mtime: Optional[float] = None     # seconds epoch, from claimedModificationTime
    sha1: Optional[str] = None
    children: list["RemoteNode"] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Sanitized canonical path used as the snapshot/diff key. Matches the
        on-disk local name (the CLI sanitizes : ? \" < > | * \\ to '_' on
        download), so remote nodes line up with local + snapshot rows."""
        return _sanitize_rel(self.path)


def _parse_time(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def _sanitize_name(name: str) -> str:
    """Mirror the CLI's download-time sanitization so remote tree keys line up
    with on-disk local names and the snapshot's canonical (sanitized) keys.
    Verified empirically: : ? " < > | * \\ -> '_'. Trailing dots/spaces kept."""
    return re.sub(r'[:?"<>|*\\]', '_', name)


def _sanitize_rel(rel: str) -> str:
    """Sanitize every path segment of a relative path."""
    return "/".join(_sanitize_name(s) for s in rel.split("/")) if rel else rel


def revision(entry: dict) -> dict:
    """The revision object from a CLI listing entry, across CLI versions.

    CLI 0.8.0 exposes it at `activeRevision`; the 0.6.0-era schema nested it one
    level deeper under `value`. Reading only the old shape silently produced
    sha1=None for every file (on 2026-09-24 all 49,734 snapshot rows had a NULL
    remote_sha1), which disabled the content-equality guard and let mtime drift
    turn into mass conflicts. Accept both shapes.
    """
    rev = entry.get("activeRevision")
    if not isinstance(rev, dict):
        return {}
    if "claimedDigests" not in rev and isinstance(rev.get("value"), dict):
        return rev["value"]
    return rev


def _entry_to_node(rel: str, entry: dict) -> RemoteNode:
    name = (entry.get("name") or {}).get("value") or entry.get("uid", "?")
    is_dir = entry.get("type") == "folder"
    # path stays RAW (used as the CLI argument); .key is the sanitized
    # canonical form used for snapshot/diff comparisons.
    node = RemoteNode(
        path=f"{rel}/{name}".strip("/"),
        name=name,
        is_dir=is_dir,
        uid=entry.get("uid", ""),
    )
    if is_dir:
        folder = entry.get("folder") or {}
        node.mtime = _parse_time(folder.get("claimedModificationTime")) or _parse_time(entry.get("modificationTime"))
    else:
        node.size = entry.get("totalStorageSize")
        node.mtime = _parse_time(entry.get("modificationTime"))
        rev = revision(entry)
        if rev:
            node.size = rev.get("claimedSize", node.size)
            node.mtime = _parse_time(rev.get("claimedModificationTime")) or node.mtime
            digests = rev.get("claimedDigests") or {}
            node.sha1 = digests.get("sha1")
    return node


def list_remote(remote_rel: str = "") -> list[RemoteNode]:
    """List immediate children of a remote dir (relative to REMOTE_ROOT)."""
    target = config.REMOTE_ROOT if not remote_rel else f"{config.REMOTE_ROOT}/{remote_rel}"
    proc = _run(["filesystem", "list", target, "-j"])
    if proc.returncode != 0:
        raise CliError(f"list {target}: {proc.stderr.strip() or proc.stdout.strip()}")
    entries = json.loads(proc.stdout or "[]")
    return [_entry_to_node(remote_rel, e) for e in entries]


def walk_remote(remote_rel: str = "", db=None, prune: bool = True) -> dict[str, RemoteNode]:
    """Recursively list the remote tree -> {relative_path: RemoteNode}.

    When `db` and `prune` are given, folders whose remote mtime matches the
    snapshot are not descended into (their known subtree is reused), turning
    the deep walk from O(all folders) into O(changed folders).
    """
    out: dict[str, RemoteNode] = {}
    # include the scope root node itself so a scoped sync doesn't conclude the
    # folder is "missing remotely" (the walk only ever lists children).
    if remote_rel:
        key = _sanitize_rel(remote_rel)
        snap = db.get(key) if db is not None else None
        out[key] = RemoteNode(path=remote_rel, name=remote_rel.rsplit("/", 1)[-1],
                              is_dir=True,
                              uid=(snap["remote_uid"] if snap else "") or "",
                              mtime=(snap["remote_mtime"] if snap else None))
    stack = [remote_rel]
    while stack:
        rel = stack.pop()
        try:
            children = list_remote(rel)
        except CliError as e:
            log.warning("walk_remote: %s", e)
            continue
        for child in children:
            # never descend into (or even record) excluded dirs — this is what
            # keeps a 13k-folder tree's walk tractable.
            if child.is_dir and _local_mod.is_excluded(child.path, is_dir=True):
                continue
            if not child.is_dir and _local_mod.is_excluded(child.path):
                continue
            # key the tree by the SANITIZED canonical path so remote nodes line
            # up with the snapshot and on-disk local names. `child.path` stays
            # raw for CLI calls (download/info/trash descend by raw path).
            out[child.key] = child
            if child.is_dir:
                descend = True
                if prune and db is not None and rel != "":
                    snap = db.get(child.key)
                    if snap is not None and snap["remote_mtime"] is not None \
                            and child.mtime is not None \
                            and abs(child.mtime - snap["remote_mtime"]) < 1.0 \
                            and snap["remote_uid"] and child.uid == snap["remote_uid"]:
                        # unchanged subtree: reuse snapshot's remote view
                        descend = False
                        for row in db.children_of(child.key):
                            if row["path"] == child.key:
                                continue
                            rp = row["path"]
                            out[rp] = RemoteNode(
                                path=rp,
                                name=rp.rsplit("/", 1)[-1],
                                is_dir=bool(row["is_dir"]),
                                uid=row["remote_uid"] or "",
                                size=row["local_size"],
                                mtime=row["remote_mtime"],
                                sha1=row["remote_sha1"],
                            )
                if descend:
                    stack.append(child.path)
    return out


_CLI_IS_08: Optional[bool] = None


def _cli_is_08_or_newer() -> bool:
    """Detect whether the bundled CLI uses the 0.8.0+ flag scheme.

    0.8.0 removed the combined `-c/--conflict-strategy` and split it into
    `-f/--file-conflict-strategy` + `-d/--folder-conflict-strategy`, and renamed
    the values (download: replace->remove, keep-both->rename). Cache the check.
    """
    global _CLI_IS_08
    if _CLI_IS_08 is None:
        try:
            proc = _run(["version"], timeout=30)
            m = re.search(r"cli-drive@(\d+)\.(\d+)\.(\d+)", proc.stdout + proc.stderr)
            _CLI_IS_08 = bool(m and (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= (0, 8, 0))
        except Exception:
            _CLI_IS_08 = True  # assume newest if detection fails
    return _CLI_IS_08


def _conflict_args(op: str, strategy: str) -> list[str]:
    """Map a logical strategy to the right CLI flags for the installed version.

    op       : 'upload' or 'download'
    strategy : 'replace' | 'keep-both' | 'skip'  (logical, version-independent)
    """
    if _cli_is_08_or_newer():
        if op == "upload":
            val = {"replace": "replace", "keep-both": "rename", "skip": "skip"}[strategy]
        else:  # download
            val = {"replace": "remove", "keep-both": "rename", "skip": "skip"}[strategy]
        return ["-f", val, "-d", val]
    return ["-c", strategy]


def upload(local_abs: str, remote_parent_rel: str, strategy: str = "replace") -> str:
    # The CLI treats the local path as a glob; escape glob metacharacters
    # ([ ] * ? \) so filenames like '[...slug].astro' match literally.
    glob_safe = re.sub(r'([\\\\*?\\[\\]])', r'\\\\\\1', local_abs)
    args = ["filesystem", "upload"] + _conflict_args("upload", strategy) + ["-t", glob_safe,
            config.REMOTE_ROOT if not remote_parent_rel else f"{config.REMOTE_ROOT}/{remote_parent_rel}"]
    proc = _run(args, timeout=3600)
    if proc.returncode != 0:
        raise CliError(f"upload {local_abs}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def download(remote_rel: str, local_parent_abs: str, strategy: str = "replace") -> str:
    args = ["filesystem", "download"] + _conflict_args("download", strategy) + [
            f"{config.REMOTE_ROOT}/{remote_rel}" if remote_rel else config.REMOTE_ROOT,
            local_parent_abs]
    proc = _run(args, timeout=3600)
    if proc.returncode != 0:
        raise CliError(f"download {remote_rel}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def create_folder(remote_parent_rel: str, name: str) -> None:
    target = config.REMOTE_ROOT if not remote_parent_rel else f"{config.REMOTE_ROOT}/{remote_parent_rel}"
    proc = _run(["filesystem", "create-folder", target, name])
    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip()
        if "already exists" in msg:
            log.debug("create-folder %s/%s: already exists (ok)", target, name)
            return
        raise CliError(f"create-folder {target}/{name}: {msg}")


def trash(remote_rel: str) -> None:
    target = f"{config.REMOTE_ROOT}/{remote_rel}" if remote_rel else config.REMOTE_ROOT
    proc = _run(["filesystem", "trash", target])
    if proc.returncode != 0:
        raise CliError(f"trash {target}: {proc.stderr.strip() or proc.stdout.strip()}")


def rename(remote_rel: str, new_name: str) -> None:
    target = f"{config.REMOTE_ROOT}/{remote_rel}"
    proc = _run(["filesystem", "rename", target, new_name])
    if proc.returncode != 0:
        raise CliError(f"rename {target}: {proc.stderr.strip() or proc.stdout.strip()}")


def info(remote_rel: str) -> Optional[dict]:
    target = f"{config.REMOTE_ROOT}/{remote_rel}" if remote_rel else config.REMOTE_ROOT
    proc = _run(["filesystem", "info", target, "-j"])
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def check_auth() -> bool:
    """Cheap health check: can we list the root?"""
    try:
        proc = _run(["filesystem", "list", "/", "-j"], timeout=30)
        return proc.returncode == 0 and proc.stdout.strip().startswith("[")
    except Exception:
        return False


def root_fingerprint() -> str:
    """Cheap change signal for the whole remote tree.

    A single `list` of the sync root. We hash the (uid, name, mtime, size) of
    every top-level entry. It can't see edits deep inside a folder, but it
    catches creates/deletes/renames at the root and is ~1 API call, so the
    daemon can run it every poll cycle and only deep-walk when it changes.
    """
    nodes = list_remote("")
    parts = sorted(f"{n.uid}|{n.name}|{n.mtime}|{n.size}" for n in nodes)
    import hashlib
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


_TRANSFER_RE = re.compile(r"(Uploaded|Downloaded|Skipped):\s*(\d+)\s*items?")


def parse_transfer_summary(text: str) -> dict[str, int]:
    out = {"uploaded": 0, "downloaded": 0, "skipped": 0}
    for m in _TRANSFER_RE.finditer(text):
        out[m.group(1).lower()] = int(m.group(2))
    return out
