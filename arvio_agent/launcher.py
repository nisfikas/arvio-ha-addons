#!/usr/bin/env python3
"""Entrypoint of the standalone agent (Home Assistant Container / NAS), where no Supervisor can
update the add-on.

Runs the agent from /data/agent-app when an OTA has put a newer one there, otherwise from the
image (/app). The agent exits 75 to be restarted on the code it just staged. A new version that
dies within its first two minutes is rolled back to the one before it, so a bad release never
leaves the house without its hub.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("ARVIO_DATA_DIR", "/data"))
LIVE = DATA / "agent-app"
PREV = DATA / "agent-app.prev"
TRIAL = DATA / "agent-app.trial"
IMAGE = Path(__file__).resolve().parent
RESTART = 75
TRIAL_S = 120


def code_dir() -> Path:
    return LIVE if (LIVE / "agent.py").is_file() else IMAGE


def rollback() -> None:
    """Back to the previous staged version, or to the image when there was none."""
    shutil.rmtree(LIVE, ignore_errors=True)
    if PREV.is_dir():
        PREV.rename(LIVE)
    TRIAL.unlink(missing_ok=True)


def run_once(here: Path) -> int:
    child = subprocess.Popen([sys.executable, str(here / "agent.py")], cwd=str(here), env={**os.environ, "ARVIO_LAUNCHER": "1"})

    def forward(signum, _frame):
        child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, forward)
    return child.wait()


def main() -> int:
    while True:
        here = code_dir()
        started = time.monotonic()
        rc = run_once(here)
        if rc == RESTART:
            continue
        if TRIAL.exists() and here == LIVE and time.monotonic() - started < TRIAL_S:
            print(f"arvio launcher: new agent exited {rc} within {TRIAL_S}s — rolling back", flush=True)
            rollback()
            continue
        return rc


if __name__ == "__main__":
    sys.exit(main())
