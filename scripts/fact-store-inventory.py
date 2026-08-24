#!/usr/bin/env python3
"""Fact store inventory — weekly health digest for memory_store.db.

Reads the hermes-memory fact store and emits a human-readable inventory:
facts per category, trust distribution, cold facts (never retrieved),
age percentiles, growth rate, and spool pipeline health.

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
    never_retrieved = _count(conn, "SELECT COUNT(*) FROM facts WHERE retrieval_count = 0")
    total_searches = _count(conn, "SELECT COALESCE(SUM(retrieval_count), 0) FROM facts")
    most_retrieved = conn.execute(
        "SELECT fact_id, content, retrieval_count FROM facts "
        "ORDER BY retrieval_count DESC LIMIT 5"
    ).fetchall()

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
        "total_searches": total_searches,
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

    lines += [
        "",
        f"**By trust tier:** 0.9+={stats['trust_tiers']['0.9+']}, "
        f"0.7={stats['trust_tiers']['0.7']}, 0.5={stats['trust_tiers']['0.5']}",
        "",
        f"**Never retrieved:** {stats['never_retrieved']} "
        f"({stats['never_retrieved_pct']:.0f}%) — cold facts, candidates for removal",
        f"**Total retrievals:** {stats['total_searches']}",
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
