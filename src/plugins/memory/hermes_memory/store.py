"""
SQLite-backed fact store with entity resolution and trust scoring.
Single-user Hermes memory store plugin.
"""

import re
import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted', 'superseded')),
    superseded_by   INTEGER REFERENCES facts(fact_id),
    superseded_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

-- Retrieval-attempt telemetry: one row per fact_store tool invocation.
-- Separates "the tool was invoked" from "a fact surfaced", so the inventory
-- can distinguish never-invoked (behavioral) from recall-miss (retriever
-- quality) — the two states retrieval_count == 0 otherwise welds together.
CREATE TABLE IF NOT EXISTS retrieval_attempts (
    attempt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    query       TEXT,
    session_id  TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_retrieval_attempts_ts ON retrieval_attempts(created_at);

-- Shadow-retrieval telemetry: one row per turn while the shadow probe is
-- enabled. matched_count/top_fact_id/top_jaccard record whether the turn had
-- high-confidence fact overlap ("opportunity"); demand_satisfied records
-- whether the agent actually invoked a retrieval action that turn. The report
-- computes opportunity-minus-demand (blind turns) from these two signals.
CREATE TABLE IF NOT EXISTS retrieval_opportunities (
    opportunity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT,
    matched_count    INTEGER NOT NULL DEFAULT 0,
    top_fact_id      INTEGER,
    top_jaccard      REAL,
    demand_satisfied INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_retrieval_opportunities_ts ON retrieval_opportunities(created_at);
"""

# Trust adjustment constants
_HELPFUL_DELTA   =  0.05
_UNHELPFUL_DELTA = -0.10
_TRUST_MIN       =  0.0
_TRUST_MAX       =  1.0

# Entity extraction patterns
_RE_CAPITALIZED  = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA          = re.compile(
    r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)',
    re.IGNORECASE,
)


def _clamp_trust(value: float) -> float:
    return max(_TRUST_MIN, min(_TRUST_MAX, value))


# fact_store actions that *ask the store for facts* (vs. writes like add/update,
# or maintenance like contradict). Used by latest_attempt_id() for shadow-probe
# demand detection — a "retrieval" is the demand the opportunity probe is
# measured against.
_RETRIEVAL_ACTIONS = ("search", "probe", "related", "reason")


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring."""

    def __init__(
        self,
        db_path: "str | Path | None" = None,
        default_trust: float = 0.5,
    ) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust = _clamp_trust(default_trust)
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables, indexes, and triggers if they do not exist. Enable WAL mode."""
        # Use the shared WAL-fallback helper so memory_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted HERMES_HOME (same issue as
        # state.db / kanban.db — see hermes_state._WAL_INCOMPAT_MARKERS).
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="memory_store.db (hermes-memory)")
        self._conn.executescript(_SCHEMA)
        # Migrate: add columns missing from pre-retraction databases (idempotent).
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "status" not in columns:
            self._conn.execute(
                "ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
                "CHECK (status IN ('active', 'retracted', 'superseded'))"
            )
        if "superseded_by" not in columns:
            self._conn.execute(
                "ALTER TABLE facts ADD COLUMN superseded_by INTEGER REFERENCES facts(fact_id)"
            )
        if "superseded_at" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN superseded_at TIMESTAMP")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_fact(
        self,
        content: str,
        category: str = "general",
        tags: str = "",
        supersedes: int | None = None,
    ) -> int:
        """Insert a fact and return its fact_id.

        Deduplicates by content (UNIQUE constraint). On duplicate, returns
        the existing fact_id without modifying the row. Extracts entities from
        the content and links them to the fact. If ``supersedes`` is given, the
        referenced fact is marked superseded atomically with this insert.
        """
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")

            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO facts (content, category, tags, trust_score)
                    VALUES (?, ?, ?, ?)
                    """,
                    (content, category, tags, self.default_trust),
                )
                fact_id: int = cur.lastrowid  # type: ignore[assignment]
                if supersedes is not None:
                    self._supersede_locked(int(supersedes), fact_id)
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                # Duplicate content — return existing id
                row = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ?", (content,)
                ).fetchone()
                return int(row["fact_id"])
            except Exception:
                # A failed supersession (e.g. cycle) must not leave the new fact half-written.
                self._conn.rollback()
                raise

            # Entity extraction and linking
            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)

            return fact_id

    def search_facts(
        self,
        query: str,
        category: str | None = None,
        min_trust: float = 0.3,
        limit: int = 10,
    ) -> list[dict]:
        """Full-text search over facts using FTS5.

        Returns a list of fact dicts ordered by FTS5 rank, then trust_score
        descending. Also increments retrieval_count for matched facts.
        """
        with self._lock:
            query = query.strip()
            if not query:
                return []

            params: list = [query, min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND f.category = ?"
                params.append(category)
            params.append(limit)

            sql = f"""
                SELECT f.fact_id, f.content, f.category, f.tags,
                       f.trust_score, f.retrieval_count, f.helpful_count,
                       f.created_at, f.updated_at, f.status, f.superseded_by
                FROM facts f
                JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ?
                  AND f.trust_score >= ?
                  AND f.status = 'active'
                  {category_clause}
                ORDER BY fts.rank, f.trust_score DESC
                LIMIT ?
            """

            rows = self._conn.execute(sql, params).fetchall()
            results = [self._row_to_dict(r) for r in rows]

            if results:
                ids = [r["fact_id"] for r in results]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})",
                    ids,
                )
                self._conn.commit()

            return results

    def record_attempt(
        self,
        action: str,
        query: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record one fact_store tool invocation.

        Best-effort telemetry: a failed write must never break the tool call
        it instruments. ``retrieval_attempts`` is created by ``_SCHEMA`` on
        ``_init_db``, so the table exists whenever a tool handler runs; the
        try/except guards the edge where the store is read-only or the table
        is absent.
        """
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO retrieval_attempts (action, query, session_id) "
                    "VALUES (?, ?, ?)",
                    (action, query, session_id),
                )
                self._conn.commit()
        except Exception:
            pass

    def latest_attempt_id(
        self,
        session_id: str | None,
        actions: tuple = _RETRIEVAL_ACTIONS,
    ) -> int:
        """Highest ``retrieval_attempts.attempt_id`` for a session among ``actions``.

        Returns 0 when there is no matching row. This is the shadow probe's
        demand signal: a new retrieval action since the previous ``sync_turn``
        means the agent *did* invoke the tool this turn. Read-only and never
        raises (mirrors ``record_attempt``'s best-effort posture).
        """
        try:
            with self._lock:
                placeholders = ",".join("?" * len(actions))
                row = self._conn.execute(
                    f"SELECT MAX(attempt_id) AS m FROM retrieval_attempts "
                    f"WHERE session_id = ? AND action IN ({placeholders})",
                    (session_id, *actions),
                ).fetchone()
                return int(row["m"] or 0)
        except Exception:
            return 0

    def record_opportunity(
        self,
        session_id: str | None = None,
        matched_count: int = 0,
        top_fact_id: int | None = None,
        top_jaccard: float | None = None,
        demand_satisfied: bool = False,
    ) -> None:
        """Record one shadow-retrieval probe result (best-effort telemetry).

        ``retrieval_opportunities`` is created by ``_SCHEMA`` on ``_init_db``;
        the try/except mirrors ``record_attempt`` so a telemetry failure never
        breaks the turn it instruments.
        """
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO retrieval_opportunities "
                    "(session_id, matched_count, top_fact_id, top_jaccard, "
                    "demand_satisfied) VALUES (?, ?, ?, ?, ?)",
                    (
                        session_id,
                        matched_count,
                        top_fact_id,
                        top_jaccard,
                        1 if demand_satisfied else 0,
                    ),
                )
                self._conn.commit()
        except Exception:
            pass

    def update_fact(
        self,
        fact_id: int,
        content: str | None = None,
        trust_delta: float | None = None,
        tags: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Partially update a fact. Trust is clamped to [0, 1].

        Returns True if the row existed, False otherwise.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            assignments: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
            params: list = []

            if content is not None:
                assignments.append("content = ?")
                params.append(content.strip())
            if tags is not None:
                assignments.append("tags = ?")
                params.append(tags)
            if category is not None:
                assignments.append("category = ?")
                params.append(category)
            if trust_delta is not None:
                new_trust = _clamp_trust(row["trust_score"] + trust_delta)
                assignments.append("trust_score = ?")
                params.append(new_trust)

            params.append(fact_id)
            self._conn.execute(
                f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?",
                params,
            )
            self._conn.commit()

            # If content changed, re-extract entities
            if content is not None:
                self._conn.execute(
                    "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
                )
                for name in self._extract_entities(content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(fact_id, entity_id)
                self._conn.commit()

            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact and its entity links. Returns True if the row existed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            self._conn.execute(
                "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
            )
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._conn.commit()
            return True

    def _chain_reaches(self, start_id: int, target_id: int, max_hops: int = 50) -> bool:
        """Follow ``superseded_by`` forward from start_id; True if target_id is reachable.

        Bounded by max_hops and a visited-set so a corrupt (cyclic) chain fails closed
        instead of looping forever. See docs/retraction-design.md §8.
        """
        visited: set[int] = set()
        cur: int = start_id
        for _ in range(max_hops):
            if cur == target_id:
                return True
            if cur in visited:
                return False  # pre-existing cycle that does not reach the target
            visited.add(cur)
            row = self._conn.execute(
                "SELECT superseded_by FROM facts WHERE fact_id = ?", (cur,)
            ).fetchone()
            if row is None or row["superseded_by"] is None:
                return False
            cur = int(row["superseded_by"])
        return False  # exceeded hop cap — treated as a cycle anomaly

    def _supersede_locked(self, fact_id: int, by_fact_id: int) -> str:
        """Mark ``fact_id`` superseded by ``by_fact_id``. Returns the superseded fact's category.

        Assumes ``self._lock`` is held and does NOT commit — the caller controls the
        transaction boundary (this is what makes ``add(supersedes=…)`` atomic).
        Raises ValueError on self-supersession, unknown ids, or a would-be cycle.
        """
        if fact_id == by_fact_id:
            raise ValueError(f"cannot supersede a fact with itself (fact_id={fact_id})")
        target = self._conn.execute(
            "SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if target is None:
            raise ValueError(f"fact_id {fact_id} does not exist")
        by_row = self._conn.execute(
            "SELECT fact_id FROM facts WHERE fact_id = ?", (by_fact_id,)
        ).fetchone()
        if by_row is None:
            raise ValueError(f"by_fact_id {by_fact_id} does not exist")
        if self._chain_reaches(by_fact_id, fact_id):
            raise ValueError(
                f"supersession would create a cycle: fact {by_fact_id} already leads to {fact_id}"
            )
        self._conn.execute(
            "UPDATE facts SET status = 'superseded', superseded_by = ?, "
            "superseded_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
            "WHERE fact_id = ?",
            (by_fact_id, fact_id),
        )
        return str(target["category"])

    def supersede_fact(self, fact_id: int, by_fact_id: int) -> dict:
        """Mark ``fact_id`` superseded by ``by_fact_id``. Raises ValueError on invalid input."""
        with self._lock:
            self._supersede_locked(fact_id, by_fact_id)
            self._conn.commit()
        return {"fact_id": fact_id, "status": "superseded", "superseded_by": by_fact_id}

    def retract_fact(self, fact_id: int) -> dict:
        """Mark a fact as no longer true (no replacement). Raises ValueError if missing."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"fact_id {fact_id} does not exist")
            self._conn.execute(
                "UPDATE facts SET status = 'retracted', superseded_by = NULL, "
                "superseded_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE fact_id = ?",
                (fact_id,),
            )
            self._conn.commit()
        return {"fact_id": fact_id, "status": "retracted"}

    def restore_fact(self, fact_id: int) -> dict:
        """Re-activate a retracted/superseded fact. Names any still-active successor.

        Restoring a mid-chain node leaves the forward tail active and detached (by design);
        the response lists it under ``still_active`` so the operator can retract it if stale.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, category, superseded_by FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"fact_id {fact_id} does not exist")
            old_successor = row["superseded_by"]
            self._conn.execute(
                "UPDATE facts SET status = 'active', superseded_by = NULL, "
                "superseded_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                (fact_id,),
            )
            self._conn.commit()
        still_active: list[dict] = []
        if old_successor is not None:
            successor = self._conn.execute(
                "SELECT fact_id, content, status FROM facts WHERE fact_id = ?", (old_successor,)
            ).fetchone()
            if successor is not None and successor["status"] == "active":
                still_active.append({"fact_id": successor["fact_id"], "content": successor["content"]})
        result: dict = {"fact_id": fact_id, "status": "active"}
        if still_active:
            result["still_active"] = still_active
        return result

    def list_facts(
        self,
        category: str | None = None,
        min_trust: float = 0.0,
        limit: int = 50,
        include_inactive: bool = False,
    ) -> list[dict]:
        """Browse facts ordered by trust_score descending.

        Optionally filter by category and minimum trust score. By default only
        active facts are returned; pass ``include_inactive=True`` to also list
        retracted/superseded facts (rows carry their ``status``).
        """
        with self._lock:
            params: list = [min_trust]
            status_clause = "" if include_inactive else "AND status = 'active'"
            category_clause = ""
            if category is not None:
                category_clause = "AND category = ?"
                params.append(category)
            params.append(limit)

            sql = f"""
                SELECT fact_id, content, category, tags, trust_score,
                       retrieval_count, helpful_count, created_at, updated_at,
                       status, superseded_by
                FROM facts
                WHERE trust_score >= ?
                  {status_clause}
                  {category_clause}
                ORDER BY trust_score DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def record_feedback(self, fact_id: int, helpful: bool) -> dict:
        """Record user feedback and adjust trust asymmetrically.

        helpful=True  -> trust += 0.05, helpful_count += 1
        helpful=False -> trust -= 0.10

        Returns a dict with fact_id, old_trust, new_trust, helpful_count.
        Raises KeyError if fact_id does not exist.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")

            old_trust: float = row["trust_score"]
            delta = _HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA
            new_trust = _clamp_trust(old_trust + delta)

            helpful_increment = 1 if helpful else 0
            self._conn.execute(
                """
                UPDATE facts
                SET trust_score    = ?,
                    helpful_count  = helpful_count + ?,
                    updated_at     = CURRENT_TIMESTAMP
                WHERE fact_id = ?
                """,
                (new_trust, helpful_increment, fact_id),
            )
            self._conn.commit()

            return {
                "fact_id":      fact_id,
                "old_trust":    old_trust,
                "new_trust":    new_trust,
                "helpful_count": row["helpful_count"] + helpful_increment,
            }

    # ------------------------------------------------------------------
    # Entity helpers
    # ------------------------------------------------------------------

    def _extract_entities(self, text: str) -> list[str]:
        """Extract entity candidates from text using simple regex rules.

        Rules applied (in order):
        1. Capitalized multi-word phrases  e.g. "John Doe"
        2. Double-quoted terms             e.g. "Python"
        3. Single-quoted terms             e.g. 'pytest'
        4. AKA patterns                    e.g. "Guido aka BDFL" -> two entities

        Returns a deduplicated list preserving first-seen order.
        """
        seen: set[str] = set()
        candidates: list[str] = []

        def _add(name: str) -> None:
            stripped = name.strip()
            if stripped and stripped.lower() not in seen:
                seen.add(stripped.lower())
                candidates.append(stripped)

        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))

        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_AKA.finditer(text):
            _add(m.group(1))
            _add(m.group(2))

        return candidates

    def _resolve_entity(self, name: str) -> int:
        """Find an existing entity by name or alias (case-insensitive) or create one.

        Returns the entity_id.
        """
        # Exact name match
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE name LIKE ?", (name,)
        ).fetchone()
        if row is not None:
            return int(row["entity_id"])

        # Search aliases — aliases stored as comma-separated; use LIKE with % boundaries
        alias_row = self._conn.execute(
            """
            SELECT entity_id FROM entities
            WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'
            """,
            (name,),
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])

        # Create new entity
        cur = self._conn.execute(
            "INSERT INTO entities (name) VALUES (?)", (name,)
        )
        self._conn.commit()
        return int(cur.lastrowid)  # type: ignore[return-value]

    def _link_fact_entity(self, fact_id: int, entity_id: int) -> None:
        """Insert into fact_entities, silently ignore if the link already exists."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO fact_entities (fact_id, entity_id)
            VALUES (?, ?)
            """,
            (fact_id, entity_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row to a plain dict."""
        return dict(row)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
