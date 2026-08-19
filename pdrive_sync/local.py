"""Local filesystem scanning (path utils, hashing, tree walk)."""
from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config


def rel_of(abs_path: Path | str) -> str:
    """Absolute local path -> POSIX relative path from SYNC_ROOT ('' for root)."""
    p = Path(abs_path).resolve()
    try:
        rel = p.relative_to(config.SYNC_ROOT)
    except ValueError:
        raise ValueError(f"path escapes sync root: {p}")
    return "" if str(rel) == "." else rel.as_posix()


def abs_of(rel: str) -> Path:
    """Relative sync path -> absolute local path, guarding against escape."""
    rel = rel.strip("/")
    candidate = (config.SYNC_ROOT / rel).resolve()
    if candidate != config.SYNC_ROOT and config.SYNC_ROOT not in candidate.parents:
        raise ValueError(f"path escapes sync root: {rel}")
    return candidate


def sha1_file(path: Path, limit: Optional[int] = None) -> Optional[str]:
    """SHA1 of file contents, or None if unreadable / over limit."""
    try:
        size = path.stat().st_size
        if limit is not None and size > limit:
            return None
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _user_excludes() -> list[str]:
    """Read patterns from SYNC_ROOT/.pdrive-ignore (rsync-style, one per line)."""
    f = config.SYNC_ROOT / config.EXCLUDE_FILE
    try:
        out = []
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
        return out
    except OSError:
        return []


def is_excluded(rel: str, extra: Optional[list[str]] = None, is_dir: bool = False) -> bool:
    name = rel.rsplit("/", 1)[-1]
    pats = config.DEFAULT_EXCLUDES + (extra or []) + _user_excludes()
    for pat in pats:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return True
    if is_dir and name in config.DEFAULT_DIR_EXCLUDES:
        return True
    return False


@dataclass
class LocalNode:
    path: str
    is_dir: bool
    size: Optional[int] = None
    mtime: Optional[float] = None
    sha1: Optional[str] = None   # only computed lazily / for files when needed


def _safe_stat(p: Path):
    try:
        return p.stat()
    except OSError:
        return None


def scan_local() -> dict[str, LocalNode]:
    """Walk SYNC_ROOT -> {relative_path: LocalNode}. Root '' is implicit.

    Keys are the LOCAL relative paths exactly as they exist on disk. Note the
    Proton CLI sanitizes some characters (: ? " < > | * \\) to '_' on download,
    so a local name may differ from its remote/snapshot name — that mapping is
    reconciled at diff time via the snapshot's stored local path, not here.

    Skips entries that vanish mid-scan or can't be stated (broken symlinks,
    permission errors) instead of aborting the whole scan.
    """
    out: dict[str, LocalNode] = {}
    root = config.SYNC_ROOT
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # don't descend into symlinked dirs (e.g. shared dirs whose target lives
        # elsewhere) — they'd be scanned as if local and confuse the diff
        dirnames[:] = [d for d in dirnames
                       if not is_excluded(d, is_dir=True)
                       and not (Path(dirpath) / d).is_symlink()]
        for d in dirnames:
            p = Path(dirpath) / d
            try:
                rel = rel_of(p)
            except ValueError:
                continue
            st = _safe_stat(p)
            if st is None:
                continue
            out[rel] = LocalNode(path=rel, is_dir=True, mtime=st.st_mtime)
        for f in filenames:
            if is_excluded(f):
                continue
            p = Path(dirpath) / f
            if p.is_symlink():
                continue  # skip symlinks (e.g. per-machine .usage.json); never sync the link or target
            try:
                rel = rel_of(p)
            except ValueError:
                continue
            st = _safe_stat(p)
            if st is None:
                continue
            out[rel] = LocalNode(path=rel, is_dir=False, size=st.st_size, mtime=st.st_mtime)
    return out
