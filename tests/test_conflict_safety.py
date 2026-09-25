"""Regression tests for the 2026-09-24 disk-fill incident.

Covers the four defects found while investigating 342 GiB of conflict copies:

  1. `proton.revision()` must read the revision object from both CLI schemas.
     Reading only the old `activeRevision.value` shape yielded sha1=None for
     every file (all 49,734 snapshot rows had a NULL remote_sha1).
  2. `_conflict()` must FETCH the remote side before moving anything locally.
     The old move-then-download order orphaned 31,979 conflict copies with no
     canonical file beside them.
  3. A conflict storm must be capped rather than allowed to fill the disk.
  4. An identical pair with no snapshot row must get a row recorded, so the
     daemon settles instead of re-hashing the file on every pass.

Run:  cd ~/pdrive/code/pdrive-sync && .venv/bin/python -m pytest tests/ -q
"""
import hashlib
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "sync"
    data = tmp_path / "data"
    root.mkdir(); data.mkdir()
    monkeypatch.setenv("PDRIVE_SYNC_ROOT", str(root))
    monkeypatch.setenv("PDRIVE_DATA_DIR", str(data))
    monkeypatch.setenv("PDRIVE_NOTIFY", "0")
    import pdrive_sync.config as config
    importlib.reload(config)
    config.ensure_dirs()
    from pdrive_sync.state import StateDB
    from pdrive_sync.local import LocalNode
    from pdrive_sync.proton import RemoteNode
    from pdrive_sync import proton, reconcile
    importlib.reload(proton)
    importlib.reload(reconcile)
    db = StateDB()
    monkeypatch.setattr(reconcile, "notify", lambda *a, **k: None)
    yield types(root, db, LocalNode, RemoteNode, reconcile, proton, monkeypatch)
    db.close()


class types:
    def __init__(self, root, db, LocalNode, RemoteNode, reconcile, proton, monkeypatch):
        self.root = root; self.db = db
        self.LocalNode = LocalNode; self.RemoteNode = RemoteNode
        self.reconcile = reconcile; self.proton = proton
        self.monkeypatch = monkeypatch

    def fresh(self):
        self.db.conn.execute("DELETE FROM nodes"); self.db.conn.commit()

    def R(self, path, is_dir=False, sha="x", size=1, mtime=1.0):
        name = path.rsplit("/", 1)[-1]
        return self.RemoteNode(path, name, is_dir, "U" + path,
                               None if is_dir else size,
                               None if is_dir else mtime,
                               None if is_dir else sha)

    def L(self, path, is_dir=False, size=1, mtime=1.0):
        return self.LocalNode(path, is_dir, None if is_dir else size,
                              None if is_dir else mtime)

    def write(self, rel, content=b"x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def conflict(self, rel, local_mtime, remote_mtime, local_size=1, remote_size=2):
        return self.reconcile.Change(rel, self.reconcile.Action.CONFLICT, False,
                                     self.L(rel, size=local_size, mtime=local_mtime),
                                     self.R(rel, size=remote_size, mtime=remote_mtime))


# --- 1. CLI schema ----------------------------------------------------------
def test_revision_reads_both_cli_shapes():
    from pdrive_sync.proton import revision
    inner = {"claimedDigests": {"sha1": "abc"}, "claimedSize": 5}
    assert revision({"activeRevision": inner}) == inner             # CLI 0.8.0
    assert revision({"activeRevision": {"value": inner}}) == inner  # 0.6.0-era
    assert revision({"activeRevision": {}}) == {}
    assert revision({}) == {}
    assert revision({"activeRevision": None}) == {}


def test_revision_yields_sha1_from_modern_shape():
    """The exact defect: sha1 was None for every file under the 0.8.0 schema."""
    from pdrive_sync.proton import revision
    entry = {"type": "file", "uid": "U", "totalStorageSize": 5,
             "activeRevision": {"claimedDigests": {"sha1": "deadbeef"},
                                "claimedSize": 5}}
    assert (revision(entry).get("claimedDigests") or {}).get("sha1") == "deadbeef"


# --- 2. fetch-before-move ---------------------------------------------------
def test_conflict_fetch_failure_leaves_local_intact(env):
    """A failed download must not move the local file or leave an orphan copy."""
    env.fresh()
    p = env.write("d/f.txt", b"local")

    def boom(*a, **k):
        raise RuntimeError("network down")

    env.monkeypatch.setattr(env.proton, "download", boom)
    env.monkeypatch.setattr(env.proton, "info", lambda *a, **k: {})

    with pytest.raises(RuntimeError):
        env.reconcile.Reconciler(env.db)._conflict(
            env.conflict("d/f.txt", local_mtime=200.0, remote_mtime=100.0))

    assert p.exists() and p.read_bytes() == b"local", "local file was disturbed"
    assert not list(p.parent.glob("*.conflict-*")), "orphan conflict copy created"
    assert not list(p.parent.glob(".pdrive-sync-tmp-*")), "scratch dir not cleaned"


def test_conflict_remote_wins_keeps_both_versions(env):
    """The remote-wins path must preserve the local copy and restore the canonical."""
    env.fresh()
    p = env.write("d/f.txt", b"local")
    os.utime(p, (100, 100))

    def fake_download(remote_rel, parent, strategy="replace"):
        Path(parent, "f.txt").write_bytes(b"remote")

    env.monkeypatch.setattr(env.proton, "download", fake_download)
    env.monkeypatch.setattr(env.proton, "info", lambda *a, **k: {})

    env.reconcile.Reconciler(env.db)._conflict(
        env.conflict("d/f.txt", local_mtime=100.0, remote_mtime=200.0))

    assert p.read_bytes() == b"remote", "canonical not restored from remote"
    copies = list(p.parent.glob("f.conflict-*"))
    assert len(copies) == 1, f"expected exactly one conflict copy, got {copies}"
    assert copies[0].read_bytes() == b"local", "local version was lost"


def test_conflict_local_wins_keeps_both_versions(env):
    env.fresh()
    p = env.write("d/f.txt", b"local")
    os.utime(p, (200, 200))

    def fake_download(remote_rel, parent, strategy="replace"):
        Path(parent, "f.txt").write_bytes(b"remote")

    env.monkeypatch.setattr(env.proton, "download", fake_download)
    env.monkeypatch.setattr(env.proton, "info", lambda *a, **k: {})

    env.reconcile.Reconciler(env.db)._conflict(
        env.conflict("d/f.txt", local_mtime=200.0, remote_mtime=100.0))

    assert p.read_bytes() == b"local", "local should keep the canonical name"
    copies = list(p.parent.glob("f.conflict-*"))
    assert len(copies) == 1 and copies[0].read_bytes() == b"remote"


def test_conflict_never_uses_replace_strategy_on_canonical(env):
    """The remote copy must be fetched to scratch, never 'replace' over the
    canonical name before the local copy has been preserved."""
    env.fresh()
    p = env.write("d/f.txt", b"local")
    os.utime(p, (100, 100))
    seen = []

    def fake_download(remote_rel, parent, strategy="replace"):
        seen.append((str(parent), strategy))
        Path(parent, "f.txt").write_bytes(b"remote")

    env.monkeypatch.setattr(env.proton, "download", fake_download)
    env.monkeypatch.setattr(env.proton, "info", lambda *a, **k: {})

    env.reconcile.Reconciler(env.db)._conflict(
        env.conflict("d/f.txt", local_mtime=100.0, remote_mtime=200.0))

    assert seen, "no download attempted"
    target_dir, strategy = seen[0]
    assert ".pdrive-sync-tmp-" in target_dir, f"fetched outside scratch: {target_dir}"
    assert strategy != "replace", "must not replace the canonical before preserving local"


# --- 3. storm brake ---------------------------------------------------------
def test_conflict_storm_is_capped(env):
    env.fresh()
    env.monkeypatch.setattr(env.reconcile.config, "MAX_CONFLICTS", 3)
    local, remote = {}, {}
    for i in range(10):
        rel = f"d/f{i}.txt"
        env.write(rel, b"local")
        local[rel] = env.L(rel, size=5, mtime=10.0)
        remote[rel] = env.R(rel, size=9, mtime=10.0, sha="not-the-local-sha")

    ch = env.reconcile.compute_changes(local, remote, env.db)

    assert not [c for c in ch if c.action == env.reconcile.Action.CONFLICT], \
        "conflict storm was not capped"


def test_conflicts_below_cap_still_apply(env):
    env.fresh()
    env.monkeypatch.setattr(env.reconcile.config, "MAX_CONFLICTS", 50)
    local, remote = {}, {}
    for i in range(3):
        rel = f"d/f{i}.txt"
        env.write(rel, b"local")
        local[rel] = env.L(rel, size=5, mtime=10.0)
        remote[rel] = env.R(rel, size=9, mtime=10.0, sha="not-the-local-sha")

    ch = env.reconcile.compute_changes(local, remote, env.db)

    assert len([c for c in ch if c.action == env.reconcile.Action.CONFLICT]) == 3


# --- 4. settle identical pairs ---------------------------------------------
def test_identical_pair_without_row_records_snapshot(env):
    """A renamed-back conflict copy has no row; one must be recorded so the
    daemon takes the fast path next pass instead of re-hashing forever."""
    env.fresh()
    body = b"same bytes"
    rel = "d/same.txt"
    env.write(rel, body)
    sha = hashlib.sha1(body).hexdigest()
    local = {rel: env.L(rel, size=len(body), mtime=1.0)}
    remote = {rel: env.R(rel, size=len(body), mtime=2.0, sha=sha)}

    env.reconcile.compute_changes(local, remote, env.db)

    row = env.db.get(rel)
    assert row is not None, "no snapshot row recorded for an identical pair"
    assert row["remote_sha1"] == sha
    assert row["local_sha1"] == sha
    assert row["local_size"] == len(body)


def test_differing_content_still_conflicts(env):
    """The snapshot recording must not mask a genuine content difference."""
    env.fresh()
    rel = "d/diff.txt"
    env.write(rel, b"local")
    local = {rel: env.L(rel, size=5, mtime=1.0)}
    remote = {rel: env.R(rel, size=5, mtime=1.0, sha="a-totally-different-sha")}

    ch = env.reconcile.compute_changes(local, remote, env.db)

    assert [c for c in ch if c.action == env.reconcile.Action.CONFLICT]