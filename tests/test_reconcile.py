"""Reconciler safety tests — especially the delete-propagation guards.

Run:  cd ~/pdrive-sync-app && .venv/bin/python -m pytest tests/test_reconcile.py -q
"""
import os
import sys
import importlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "sync"
    data = tmp_path / "data"
    root.mkdir(); data.mkdir()
    monkeypatch.setenv("PDRIVE_SYNC_ROOT", str(root))
    monkeypatch.setenv("PDRIVE_DATA_DIR", str(data))
    import pdrive_sync.config as config
    importlib.reload(config)
    config.ensure_dirs()
    from pdrive_sync.state import StateDB
    from pdrive_sync.local import LocalNode
    from pdrive_sync.proton import RemoteNode
    from pdrive_sync import reconcile
    importlib.reload(reconcile)
    db = StateDB()
    yield types(root, db, LocalNode, RemoteNode, reconcile)
    db.close()


class types:
    def __init__(self, root, db, LocalNode, RemoteNode, reconcile):
        self.root = root; self.db = db
        self.LocalNode = LocalNode; self.RemoteNode = RemoteNode
        self.reconcile = reconcile

    def fresh(self):
        self.db.conn.execute("DELETE FROM nodes"); self.db.conn.commit()

    def R(self, path, is_dir=False, sha="x", size=1, mtime=1.0):
        name = path.rsplit("/", 1)[-1]
        return self.RemoteNode(path, name, is_dir, "U" + path, None if is_dir else size,
                               None if is_dir else mtime, None if is_dir else sha)

    def L(self, path, is_dir=False, size=1, mtime=1.0):
        return self.LocalNode(path, is_dir, None if is_dir else size, None if is_dir else mtime)

    def write(self, rel, content=b"x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p


def _acts(changes):
    return {(c.path, c.action.value) for c in changes}


# --- the Music bug ----------------------------------------------------------
def test_scoped_resync_empty_local_scan_never_deletes(env):
    """The exact Music scenario: scoped resync, empty local scan, snapshot+remote
    have the folder. Must recreate locally, never delete remote."""
    env.fresh()
    env.db.upsert("Music", True)
    env.db.upsert("Music/a.txt", False, local_sha1="x", local_size=3, local_mtime=1,
                  remote_uid="U", remote_sha1="x", remote_mtime=1)
    env.db.conn.commit()
    remote = {"Music": env.R("Music", True), "Music/a.txt": env.R("Music/a.txt", size=3)}
    ch = env.reconcile.compute_changes({}, remote, env.db, scope="Music", trusted_local=False)
    acts = _acts(ch)
    assert not any(a.startswith("delete") for _, a in acts), f"would delete: {acts}"
    assert ("Music", "mkdir_local") in acts and ("Music/a.txt", "download") in acts


def test_trusted_full_scan_real_local_delete_propagates(env):
    """A genuine delete of a FILE (full trusted scan, really gone) must delete
    remote. Directories are never scan-deleted (watcher handles those)."""
    env.fresh()
    env.db.upsert("Music", True)
    env.db.upsert("Music/a.txt", False, local_sha1="x", local_size=3, local_mtime=1,
                  remote_uid="U", remote_sha1="x", remote_mtime=1)
    env.db.conn.commit()
    # local scan: Music dir present, but a.txt is genuinely gone
    local = {"Music": env.L("Music", True)}
    remote = {"Music": env.R("Music", True), "Music/a.txt": env.R("Music/a.txt", size=3)}
    ch = env.reconcile.compute_changes(local, remote, env.db, scope="", trusted_local=True)
    acts = _acts(ch)
    assert ("Music/a.txt", "delete_remote") in acts
    # the now-empty dir is NOT scan-deleted (safe default)
    assert ("Music", "delete_remote") not in acts


def test_untrusted_partial_scan_subdir_vanishes_no_delete(env):
    """Trusted scan, but a subdir is missing while its parent shows -> the subdir
    and its files must NOT be deleted (scan was partial/unreadable)."""
    env.fresh()
    env.db.upsert("Big", True)
    env.db.upsert("Big/sub", True)
    env.db.upsert("Big/sub/f.txt", False, local_sha1="x", local_size=1, local_mtime=1,
                  remote_uid="U", remote_sha1="x", remote_mtime=1)
    env.db.conn.commit()
    # local scan shows Big but NOT Big/sub (subdir unreadable / vanished mid-scan)
    local = {"Big": env.L("Big", True)}
    remote = {"Big": env.R("Big", True), "Big/sub": env.R("Big/sub", True),
              "Big/sub/f.txt": env.R("Big/sub/f.txt")}
    ch = env.reconcile.compute_changes(local, remote, env.db, trusted_local=True)
    acts = _acts(ch)
    # the file's ancestor Big/sub is absent from the scan -> untrusted -> no delete
    assert not any(a.startswith("delete") for _, a in acts), f"would delete: {acts}"


# --- MAX_DELETE counts files, not paths --------------------------------------
def test_max_delete_counts_files_not_paths(env):
    """A single directory delete affecting >MAX_DELETE files must be blocked."""
    env.fresh()
    for i in range(60):
        env.write(f"D/f{i}.txt", b"x")
        env.db.upsert(f"D/f{i}.txt", False, local_sha1="x", local_size=1, local_mtime=1,
                      remote_uid=f"U{i}", remote_sha1="x", remote_mtime=1)
    env.db.upsert("D", True); env.db.conn.commit()
    local = {f"D/f{i}.txt": env.L(f"D/f{i}.txt") for i in range(60)}
    local["D"] = env.L("D", True)
    ch = env.reconcile.compute_changes(local, {}, env.db, force=False)  # remote empty
    assert not any(c.action.value == "delete_local" for c in ch), "mass local delete not blocked"


# --- core directions still work ----------------------------------------------
def test_local_edit_uploads(env):
    env.fresh()
    p = env.write("a.txt", b"hello world")  # real file so SHA check works
    st = p.stat()
    now = st.st_mtime
    # snapshot matches the CURRENT local content, remote is older/different
    import hashlib
    lsha = hashlib.sha1(b"hello world").hexdigest()
    env.db.upsert("a.txt", False, local_sha1=lsha, local_size=st.st_size, local_mtime=now,
                  remote_uid="U", remote_sha1=lsha, remote_mtime=now - 50)
    env.db.conn.commit()
    local = {"a.txt": env.L("a.txt", size=st.st_size, mtime=now)}
    # remote has NEW content (different sha from snapshot) -> must download
    remote = {"a.txt": env.R("a.txt", size=99, mtime=now, sha="newsha")}
    ch = env.reconcile.compute_changes(local, remote, env.db, trusted_local=True)
    acts = _acts(ch)
    assert ("a.txt", "download") in acts


def test_sanitized_local_name_not_duplicated(env):
    """A file the CLI downloaded under a sanitized name ('Song: x' -> 'Song_ x')
    must be recognised as the SAME node as its remote/snapshot path — not a
    phantom 'new local' + 'missing local' pair."""
    import hashlib
    env.fresh()
    content = b"track audio data"
    lsha = hashlib.sha1(content).hexdigest()
    # snapshot records remote path 'Song: x.flac' but the on-disk local_path is
    # the sanitized 'Song_ x.flac' (what the CLI actually wrote)
    env.write("Music/Song_ x.flac", content)  # sanitized name on disk
    env.db.upsert("Music", True, remote_uid="UM", remote_mtime=1.0)
    env.db.upsert("Music/Song: x.flac", False, local_sha1=lsha, local_size=len(content),
                  local_mtime=1.0, remote_uid="U", remote_sha1=lsha, remote_mtime=1.0,
                  local_path="Music/Song_ x.flac")
    env.db.conn.commit()
    # local scan sees the sanitized name (and the Music dir); remote has the real name
    st = (env.root / "Music/Song_ x.flac").stat()
    local = {"Music": env.L("Music", True),
             "Music/Song_ x.flac": env.L("Music/Song_ x.flac", size=st.st_size, mtime=st.st_mtime)}
    remote = {"Music": env.R("Music", True),
              "Music/Song: x.flac": env.R("Music/Song: x.flac", size=len(content), mtime=1.0, sha=lsha)}
    ch = env.reconcile.compute_changes(local, remote, env.db, trusted_local=True)
    acts = _acts(ch)
    # should be a no-op (in sync), NOT upload-new + download-missing
    assert ("Music/Song_ x.flac", "upload") not in acts, f"phantom upload: {acts}"
    assert ("Music/Song: x.flac", "download") not in acts, f"phantom download: {acts}"
    assert not acts, f"expected no changes, got {acts}"


def test_block_logs_each_delete_path(env, caplog):
    """When MAX_DELETE blocks, the log must name each path so the user can judge
    legitimacy (not just a bare count)."""
    import logging
    env.fresh()
    for i in range(60):
        env.write(f"D/f{i}.txt", b"x")
        env.db.upsert(f"D/f{i}.txt", False, local_sha1="x", local_size=1, local_mtime=1,
                      remote_uid=f"U{i}", remote_sha1="x", remote_mtime=1)
    env.db.upsert("D", True); env.db.conn.commit()
    local = {f"D/f{i}.txt": env.L(f"D/f{i}.txt") for i in range(60)}
    local["D"] = env.L("D", True)
    with caplog.at_level(logging.ERROR):
        env.reconcile.compute_changes(local, {}, env.db, force=False)
    # every blocked path must appear in the log
    assert "Blocked delete paths" in caplog.text
    assert "D/f0.txt" in caplog.text and "D/f59.txt" in caplog.text
