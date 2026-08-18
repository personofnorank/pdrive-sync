"""Logging + desktop notifications."""
from __future__ import annotations

import logging
import os
import subprocess

from . import config


def get_logger() -> logging.Logger:
    logger = logging.getLogger("pdrive-sync")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    config.ensure_dirs()

    fh = logging.FileHandler(config.LOG_PATH)
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)
    return logger


def notify(summary: str, body: str = "", urgency: str = "normal") -> None:
    """Desktop popup; uses the DBUS injection that works on COSMIC/Wayland."""
    if not config.NOTIFY:
        return
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    try:
        subprocess.run(
            ["notify-send", "-u", urgency, "-a", "pdrive-sync", summary, body],
            env=env,
            timeout=5,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
