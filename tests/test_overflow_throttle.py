import threading, time
from pdrive_sync import daemon


import pytest


@pytest.mark.skipif(not hasattr(__import__("watchdog.observers.inotify", fromlist=["x"]), "InotifyWatchGroup"), reason="needs watchdog>=6 with InotifyWatchGroup")
def test_overflow_rebuilds_are_coalesced(monkeypatch):
    from watchdog.observers import inotify as ino
    calls = []
    monkeypatch.setattr(ino.InotifyWatchGroup, "on_queue_overflow",
                        lambda self: calls.append(1))
    seen = []
    assert daemon._throttle_overflow_rebuild(lambda: seen.append(1), min_interval=0.3)
    g = ino.InotifyWatchGroup.__new__(ino.InotifyWatchGroup)
    for _ in range(50):
        g.on_queue_overflow()
    assert len(seen) == 50          # every overflow still triggers a sync cycle
    assert len(calls) == 1          # but only one immediate rebuild
    time.sleep(0.6)
    assert len(calls) == 2          # plus exactly one trailing rebuild
