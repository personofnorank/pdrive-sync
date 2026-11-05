"""Keep the test suite away from the real app/data/sync directories.

pdrive_sync modules call `get_logger()` at import time, which resolves
config.LOG_PATH and runs config.ensure_dirs() BEFORE any fixture can set
PDRIVE_DATA_DIR. Without this, merely running pytest created
~/pdrive-sync-app/data and appended test output to its pdrive-sync.log.

This file runs before the test modules are imported, so pointing the path
env vars at a throwaway directory here covers the import-time logger. It
overrides (not setdefault) any value already in the environment, so a shell
that exports the real PDRIVE_APP_DIR cannot make tests write to live data.
"""
import os
import shutil
import tempfile

_ISOLATED = tempfile.mkdtemp(prefix="pdrive-sync-tests-")
os.environ["PDRIVE_APP_DIR"] = os.path.join(_ISOLATED, "app")
os.environ["PDRIVE_DATA_DIR"] = os.path.join(_ISOLATED, "app", "data")
os.environ["PDRIVE_SYNC_ROOT"] = os.path.join(_ISOLATED, "sync")
os.environ["PDRIVE_NOTIFY"] = "0"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_ISOLATED, ignore_errors=True)
