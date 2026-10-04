#!/usr/bin/env python3
"""Fact store inventory — weekly health digest for memory_store.db.

Reads the holographic memory store and emits a human-readable inventory:
facts per category, trust distribution, cold facts (never surfaced, split
into never-invoked vs recall-miss), retrieval telemetry (invocations vs
surfacings), age percentiles, growth rate, and spool pipeline health.

Usage:
    python3 fact-store-inventory.py [--db PATH] [--spool PATH] [--json]

Cron (no_agent):
    hermes cron create --schedule "0 9 * * 0" --name "fact-store-inventory"
        --no-agent --script ~/.hermes/scripts/fact-store-inventory.py

Exit codes:
    0  — OK
    1  — DB not found or unreadable
    2  — Spool has queued lines older than 6 hours (stalled ingest)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Actions that constitute "retrieval" (as opposed to write/CRUD actions).
# Matches the action set the hermes-memory plugin records in retrieval_attempts.
_RETRIEVAL_ACTIONS = ("search", "probe", "related", "reason", "contradict")


def get_default_paths() -> tuple[Path, Path]:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    db = hermes_home / "memory_store.db"
    spool = hermes_home / "memory_spool.jsonl"
    return db, spool


def _count(db: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return db.execute(sql, params).fetchone()[0]


def collect_stats(db_path: Path, spool_path: Path) -> dict:
    if not db_path.exists():
        print(f"ERROR: memory_store.db not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    fortnight_ago = now - timedelta(days=14)

    # ── Basic counts ──────────────────────────────────────────────────
    total = _count(conn, "SELECT COUNT(*) FROM facts")

    cats = {}
    for row in conn.execute(
        "SELECT category, COUNT(*) n FROM facts GROUP BY category ORDER BY n DESC"
    ):
        cats[row["category"]] = row["n"]

    tiers = {}
    for row in conn.execute(
        "SELECT "
        "  SUM(CASE WHEN trust_score >= 0.9 THEN 1 ELSE 0 END) AS t09,"
        "  SUM(CASE WHEN trust_score >= 0.7 AND trust_score < 0.9 THEN 1 ELSE 0 END) AS t07,"
        "  SUM(CASE WHEN trust_score < 0.7 THEN 1 ELSE 0 END) AS t05 "
        "FROM facts"
    ):
        tiers = {"0.9+": row["t09"] or 0, "0.7": row["t07"] or 0, "0.5": row["t05"] or 0}

    # ── Retrieval health ──────────────────────────────────────────────
    # retrieval_count == 0 ("cold") welds two states into one number:
    #   (1) never invoked  — the agent never called the retrieval tool
    #   (2) recall miss    — the tool was called but this fact never ranked top-K
    # The retrieval_attempts table (added by the plugin) disambiguates them.
    never_retrieved = _count(conn, "SELECT COUNT(*) FROM facts WHERE retrieval_count = 0")
    surfacings = _count(conn, "SELECT COALESCE(SUM(retrieval_count), 0) FROM facts")
    surfaced_unconfirmed = _count(
        conn,
        "SELECT COUNT(*) FROM facts WHERE retrieval_count > 0 AND helpful_count = 0",
    )
    most_retrieved = conn.execute(
        "SELECT fact_id, content, retrieval_count, helpful_count FROM facts "
        "ORDER BY retrieval_count DESC LIMIT 5"
    ).fetchall()

    # retrieval_attempts may be absent if the plugin has not been re-deployed
    # since the schema change — fall back to 0 (and report "never-invoked").
    invocations = 0
    invocations_total = 0
    has_attempts = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retrieval_attempts'"
        ).fetchone()
        is not None
    )
    if has_attempts:
        placeholders = ",".join("?" * len(_RETRIEVAL_ACTIONS))
        invocations = _count(
            conn,
            f"SELECT COUNT(*) FROM retrieval_attempts WHERE action IN ({placeholders})",
            _RETRIEVAL_ACTIONS,
        )
        invocations_total = _count(conn, "SELECT COUNT(*) FROM retrieval_attempts")

    # ── Age distribution ──────────────────────────────────────────────
    ages_days = [
        row[0] for row in conn.execute(
            "SELECT CAST(julianday('now') - julianday(created_at) AS INTEGER) FROM facts"
        )
    ]
    ages_days.sort()
    if ages_days:
        p50 = ages_days[len(ages_days) // 2]
        p90 = ages_days[int(len(ages_days) * 0.9)]
        age_max = ages_days[-1]
    else:
        p50 = p90 = age_max = 0

    # ── Growth rate ───────────────────────────────────────────────────
    this_week = _count(
        conn,
        "SELECT COUNT(*) FROM facts WHERE created_at >= ?",
        (week_ago.strftime("%Y-%m-%d"),),
    )
    last_week = _count(
        conn,
        "SELECT COUNT(*) FROM facts WHERE created_at >= ? AND created_at < ?",
        (fortnight_ago.strftime("%Y-%m-%d"), week_ago.strftime("%Y-%m-%d")),
    )

    # ── Contradictions ────────────────────────────────────────────────
    contradiction_count = _count(
        conn,
        "SELECT COUNT(*) FROM facts WHERE tags LIKE '%contradiction%'",
    )

    conn.close()

    # ── Spool health ──────────────────────────────────────────────────
    spool_lines = 0
    spool_stalled = False
    spool_oldest_age_h = 0.0
    if spool_path.exists():
        try:
            with open(spool_path) as f:
                spool_lines = sum(1 for _ in f)
            if spool_lines > 0:
                spool_mtime = datetime.fromtimestamp(
                    spool_path.stat().st_mtime, tz=timezone.utc
                )
                spool_oldest_age_h = (now - spool_mtime).total_seconds() / 3600
                spool_stalled = spool_oldest_age_h > 6
        except OSError:
            pass

    # ── Dedup threshold ───────────────────────────────────────────────
    _DEDUP_THRESHOLD = 500
    dedup_pct = total / _DEDUP_THRESHOLD * 100

    return {
        "total": total,
        "categories": cats,
        "trust_tiers": tiers,
        "never_retrieved": never_retrieved,
        "never_retrieved_pct": never_retrieved / total * 100 if total else 0,
        "surfacings": surfacings,
        "invocations": invocations,
        "invocations_total": invocations_total,
        "surfaced_unconfirmed": surfaced_unconfirmed,
        "most_retrieved": [dict(r) for r in most_retrieved],
        "age_p50": p50,
        "age_p90": p90,
        "age_max": age_max,
        "added_this_week": this_week,
        "added_last_week": last_week,
        "contradiction_count": contradiction_count,
        "spool_lines": spool_lines,
        "spool_stalled": spool_stalled,
        "spool_oldest_age_h": spool_oldest_age_h,
        "dedup_pct": dedup_pct,
        "dedup_threshold": _DEDUP_THRESHOLD,
    }


def format_stats(stats: dict) -> str:
    lines = [
        "## Fact Store Inventory",
        "",
        f"**Total facts:** {stats['total']}",
        "",
        "| Category | Count |",
        "|---|---|",
    ]
    for cat, n in sorted(stats["categories"].items(), key=lambda x: -x[1]):
        lines.append(f"| {cat} | {n} |")

    # Split "cold" (retrieval_count == 0) into never-invoked vs recall-miss.
    # invocations == 0 means the retrieval tool has never been called since the
    # attempt counter was added, so every cold fact is "never given a chance"
    # (a behavioral signal) rather than "tested and rejected" (a fact-quality
    # signal). Only the latter is a pruning signal.
    if stats["invocations"] == 0:
        cold_note = (
            "never-invoked — the retrieval tool has not been called since the "
            "attempt counter was added; cold here is a behavioral signal, not a "
            "fact-quality signal"
        )
    else:
        cold_note = (
            f"recall-miss — never surfaced despite {stats['invocations']} "
            "retrieval invocations"
        )

    lines += [
        "",
        f"**By trust tier:** 0.9+={stats['trust_tiers']['0.9+']}, "
        f"0.7={stats['trust_tiers']['0.7']}, 0.5={stats['trust_tiers']['0.5']}",
        "",
        "**Retrieval telemetry:**",
        f"  - Invocations (search/probe/related/reason/contradict): {stats['invocations']}",
        f"  - Surfacings (Σ retrieval_count): {stats['surfacings']}",
        f"  - Cold facts (never surfaced): {stats['never_retrieved']} "
        f"({stats['never_retrieved_pct']:.0f}%) — {cold_note}",
        f"  - Surfaced but unconfirmed (helpful_count == 0): {stats['surfaced_unconfirmed']} "
        "(proxy for 'surfaced and ignored')",
        "",
        f"**Age:** p50={stats['age_p50']}d, p90={stats['age_p90']}d, max={stats['age_max']}d",
        f"**Growth:** +{stats['added_this_week']} this week "
        f"(prev: {stats['added_last_week']})",
    ]

    if stats["contradiction_count"] > 0:
        lines.append(f"**⚠ Contradictions flagged:** {stats['contradiction_count']}")

    lines += [
        "",
        f"**Spool:** {stats['spool_lines']} queued lines"
    ]
    if stats["spool_stalled"]:
        lines.append(f"  ⚠ STALLED — oldest line {stats['spool_oldest_age_h']:.1f}h old (ingest hook may be down)")
    elif stats["spool_lines"] > 0:
        lines.append(f"  OK — oldest line {stats['spool_oldest_age_h']:.1f}h old")
    else:
        lines.append("  OK — spool empty")

    lines += [
        "",
        f"**⚠ Dedup threshold:** {stats['total']}/{stats['dedup_threshold']} "
        f"facts ({stats['dedup_pct']:.0f}%)",
    ]

    if stats["most_retrieved"]:
        lines += ["", "**Top retrieved:**"]
        for r in stats["most_retrieved"]:
            content = r["content"][:80]
            lines.append(f"  - [{r['retrieval_count']}×] {content}")

    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Fact store inventory")
    parser.add_argument("--db", help="Path to memory_store.db")
    parser.add_argument("--spool", help="Path to memory_spool.jsonl")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    db_path, spool_path = get_default_paths()
    if args.db:
        db_path = Path(args.db)
    if args.spool:
        spool_path = Path(args.spool)

    stats = collect_stats(db_path, spool_path)

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        print(format_stats(stats))

    if stats["spool_stalled"]:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
