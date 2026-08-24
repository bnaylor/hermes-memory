# Fact Store Durability Backup — Design

**Status:** Approved (scromp, 2026-08-24) · implementing
**Author:** Clomp
**Closes:** Gap #6 in `docs/memory-gap-closure.md` (2026-08-24 Rune DB-loss incident)

## Problem

Each agent's `~/.hermes/memory_store.db` (the hermes-memory fact store) holds the
agent's local facts (~150–370). It has no scheduled backup. The 2026-08-24
incident — Rune accidentally wiped his DB and lost ~2/3 of 370 facts — exposed
that the documented recovery path ("hydrate via JetStream replay") was never a
real backup: the `agent-memory` stream only ever carried ~6 shared facts, and
those aged out past the 7-day retention window.

**Root principle: JetStream is transport, not backup.**

## Goals

- A scheduled, *consistent* snapshot of `memory_store.db` to durable NFS storage.
- Cap data loss at ≤ 1 day (vs. ~2/3 of the store).
- Zero token cost (a `no_agent` script, watchdog semantics).
- Self-verifying — a torn or empty snapshot must never finalize.

## Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Snapshot method | `VACUUM INTO` | Consistent single-file copy, WAL-safe, no sidecar `-wal`/`-shm` needed. Validated: `integrity_check: ok`, 156/156 rows. |
| Scope | `memory_store.db` only | Holds `facts` + `okf_ingestion_state` — one file = full recovery. The 0-byte `fact_store.db`/`memory.db` are stale artifacts. |
| Target | `/shared/agents/<agent>/backups/fact-store/` | Per-agent subdir (NFS write isolation); agent name derived from OS uid (`pwd.getpwuid`). |
| Privacy | `700` owner-only dir, `600` snapshot | A full fact-store dump contains personal/financial facts; cross-agent read buys nothing. |
| Atomicity | `VACUUM INTO` → `.tmp` → verify → `os.replace` | Both files on NFS (same FS), so the rename is atomic; no cross-device rename. |
| Cadence | Daily `0 3 * * *` | ~250 KB / <1 s; caps loss at a day. Weekly would lose up to 7 days of fresh facts. |
| Retention | Keep last 7 | A week of dailies recovers from any nuke; longer is dead weight. |
| WAL checkpoint | None | `VACUUM INTO` already yields a consistent copy; adding `wal_checkpoint` would add a write step to a read-only job. |

## Script (`scripts/fact-store-backup.py`)

- Read-only source connection (`mode=ro`) + `busy_timeout=5000`.
- `VACUUM INTO` a `.tmp` in the target dir.
- Verify: `PRAGMA integrity_check == ok` + `facts` row count.
- `os.replace` to `memory_store-YYYYMMDD-HHMMSS.db`, `chmod 600`.
- Prune to last 7.
- Watchdog semantics: exit 0 + empty stdout (silent) on success; exit non-zero +
  error on stdout on failure.

Exit codes: `0` OK · `1` source DB missing · `2` VACUUM/verify failed · `3` NFS target unavailable.

## Restore procedure

```bash
# 1. Stop writes (pause the ingest hook / idle the gateway) so the DB is quiescent.
# 2. Remove WAL sidecars so they don't overlay the restored snapshot.
rm -f ~/.hermes/memory_store.db-wal ~/.hermes/memory_store.db-shm
# 3. Copy a snapshot back.
cp /shared/agents/<agent>/backups/fact-store/memory_store-<pick>.db ~/.hermes/memory_store.db
chmod 600 ~/.hermes/memory_store.db
# 4. Verify.
python3 -c "import sqlite3; c=sqlite3.connect('$HOME/.hermes/memory_store.db'); print(c.execute('SELECT COUNT(*) FROM facts').fetchone()[0])"
```

## Deployment

The script deploys like the existing `fact-store-inventory.py`:

```bash
cp scripts/fact-store-backup.py ~/.hermes/scripts/
hermes cron create --schedule "0 3 * * *" --name "fact-store-backup" \
    --no-agent --script fact-store-backup.py
```

## Not in scope

- `pre-upgrade-backup` (config/skills/plugins) — separate, event-driven, one-shot.
- Other Hermes DBs (`state.db`, `scheduler.db`) — different durability/recoverability profile.
- Cross-agent restore (each agent restores its own backup).

## Relation to Gap #6

This design is the implementation of Gap #6. The inter-agent memory doc
(`fact-store-discipline` skill) is updated to record "JetStream is transport, not
backup" so the false-recovery assumption doesn't recur.
