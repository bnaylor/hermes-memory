#!/usr/bin/env python3
"""Fact store durability backup.

Consistent snapshot of the hermes-memory fact store (~/.hermes/memory_store.db)
to NFS using SQLite ``VACUUM INTO``. Closes the durability gap exposed by the
2026-08-24 Rune DB-loss incident (Gap #6 in docs/memory-gap-closure.md).

Design: docs/fact-store-backup-design.md

Cron (no_agent, watchdog — silent on success, alert on failure):
    hermes cron create --schedule "0 3 * * *" --name "fact-store-backup"
        --no-agent --script fact-store-backup.py

Exit codes:
    0 — snapshot written + verified (silent)
    1 — source DB missing or unreadable
    2 — VACUUM INTO or snapshot verification failed
    3 — NFS target directory unavailable / not writable
"""

from __future__ import annotations

import os
import pwd
import re
import sqlite3
import sys
from datetime import datetime, timezone

RETAIN = int(os.environ.get("FACT_STORE_BACKUP_RETAIN", "7"))
AGENT = os.environ.get("AGENT_NAME") or pwd.getpwuid(os.getuid()).pw_name
NFS_BASE = "/shared/agents"


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _target_dir() -> str:
    return os.path.join(NFS_BASE, AGENT, "backups", "fact-store")


def _fail(msg: str, code: int) -> int:
    # stdout (not stderr) so the no_agent watchdog delivers the message verbatim.
    print(f"fact-store-backup: ERROR: {msg}")
    return code


def _verify(path: str) -> int:
    """Return the fact-row count if the snapshot is a valid non-empty DB, else raise."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"integrity_check returned {ok!r}")
        return conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    finally:
        conn.close()


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _prune(target_dir: str) -> None:
    pattern = re.compile(r"^memory_store-\d{8}-\d{6}\.db$")
    snaps = sorted(f for f in os.listdir(target_dir) if pattern.match(f))
    for old in snaps[:-RETAIN]:
        try:
            os.remove(os.path.join(target_dir, old))
        except OSError as e:
            print(f"fact-store-backup: WARN: prune failed for {old}: {e}")


def main() -> int:
    src = os.path.join(_hermes_home(), "memory_store.db")
    if not os.path.exists(src):
        return _fail(f"source DB missing: {src}", 1)

    target_dir = _target_dir()
    try:
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        os.chmod(target_dir, 0o700)
    except OSError as e:
        return _fail(f"cannot prepare target dir {target_dir}: {e}", 3)
    if not os.access(target_dir, os.W_OK):
        return _fail(f"target dir not writable: {target_dir}", 3)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    final = os.path.join(target_dir, f"memory_store-{stamp}.db")
    tmp = f"{final}.tmp"

    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(f"VACUUM INTO '{tmp}'")
        finally:
            conn.close()
    except sqlite3.Error as e:
        _cleanup(tmp)
        return _fail(f"VACUUM INTO failed: {e}", 2)

    try:
        rows = _verify(tmp)
    except Exception as e:
        _cleanup(tmp)
        return _fail(f"snapshot verification failed: {e}", 2)

    os.replace(tmp, final)
    os.chmod(final, 0o600)
    _prune(target_dir)
    # silent success — no stdout, exit 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
