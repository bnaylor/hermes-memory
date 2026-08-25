# tests/test_retraction.py
"""Tests for the retraction primitive (retract / supersede / restore).

Maps to docs/retraction-design.md §10. Covers the state machine,
suppression across recall paths, reversibility, and the CHECK constraint.
"""

import json
import sqlite3

import pytest

from plugins.memory.hermes_memory.retrieval import FactRetriever
from plugins.memory.hermes_memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "mem.db"))


@pytest.fixture
def retriever(store):
    return FactRetriever(store=store)


def _fact_ids(results):
    return {r["fact_id"] for r in results}


class TestStateMachine:
    def test_retract_sets_status_and_clears_pointer(self, store):
        fid = store.add_fact("Service Account Key rotates every 90 days", category="general")
        result = store.retract_fact(fid)
        assert result["status"] == "retracted"
        row = store._conn.execute(
            "SELECT status, superseded_by FROM facts WHERE fact_id = ?", (fid,)
        ).fetchone()
        assert row["status"] == "retracted"
        assert row["superseded_by"] is None

    def test_supersede_sets_status_and_pointer(self, store):
        a = store.add_fact("Service Account Key rotates every 90 days", category="general")
        b = store.add_fact("Service Account Key rotates every 30 days", category="general")
        result = store.supersede_fact(a, b)
        assert result["status"] == "superseded"
        assert result["superseded_by"] == b
        row = store._conn.execute(
            "SELECT status, superseded_by FROM facts WHERE fact_id = ?", (a,)
        ).fetchone()
        assert row["status"] == "superseded"
        assert row["superseded_by"] == b

    def test_supersede_rejects_self(self, store):
        a = store.add_fact("Service Account Key rotates every 90 days", category="general")
        with pytest.raises(ValueError):
            store.supersede_fact(a, a)

    def test_supersede_rejects_unknown_by(self, store):
        a = store.add_fact("Service Account Key rotates every 90 days", category="general")
        with pytest.raises(ValueError):
            store.supersede_fact(a, 999999)

    def test_supersede_rejects_cycle(self, store):
        a = store.add_fact("Service Account Key rotates every 90 days", category="general")
        b = store.add_fact("Service Account Key rotates every 30 days", category="general")
        store.supersede_fact(a, b)  # a -> b
        with pytest.raises(ValueError):
            store.supersede_fact(b, a)  # would close a cycle

    def test_restore_flips_to_active(self, store):
        fid = store.add_fact("Service Account Key rotates every 90 days", category="general")
        store.retract_fact(fid)
        result = store.restore_fact(fid)
        assert result["status"] == "active"
        row = store._conn.execute(
            "SELECT status, superseded_by FROM facts WHERE fact_id = ?", (fid,)
        ).fetchone()
        assert row["status"] == "active"
        assert row["superseded_by"] is None

    def test_restore_mid_chain_names_orphan(self, store):
        a = store.add_fact("Service Account Key rotates every 90 days", category="general")
        b = store.add_fact("Service Account Key rotates every 30 days", category="general")
        c = store.add_fact("Service Account Key rotates every 14 days", category="general")
        store.supersede_fact(a, b)
        store.supersede_fact(b, c)
        result = store.restore_fact(b)
        assert result["status"] == "active"
        # c was superseding b and is still active -> named in the response
        orphan_ids = {f["fact_id"] for f in result.get("still_active", [])}
        assert c in orphan_ids
        # c is still active (not auto-retracted)
        crow = store._conn.execute(
            "SELECT status FROM facts WHERE fact_id = ?", (c,)
        ).fetchone()
        assert crow["status"] == "active"

    def test_add_supersedes_atomic(self, store):
        old = store.add_fact("Service Account Key rotates every 90 days", category="general")
        new = store.add_fact(
            "Service Account Key rotates every 30 days", category="general", supersedes=old
        )
        old_row = store._conn.execute(
            "SELECT status, superseded_by FROM facts WHERE fact_id = ?", (old,)
        ).fetchone()
        new_row = store._conn.execute(
            "SELECT status FROM facts WHERE fact_id = ?", (new,)
        ).fetchone()
        assert old_row["status"] == "superseded"
        assert old_row["superseded_by"] == new
        assert new_row["status"] == "active"

    def test_add_supersedes_invalid_rolls_back(self, store):
        before = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        with pytest.raises(ValueError):
            store.add_fact("Service Account Key rotates every 30 days", supersedes=999999)
        after = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        assert after == before  # no orphaned insert left behind

    def test_check_constraint_rejects_invalid_status(self, store):
        fid = store.add_fact("Service Account Key rotates every 90 days", category="general")
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "UPDATE facts SET status = 'bogus' WHERE fact_id = ?", (fid,)
            )

    def test_migration_is_idempotent(self, store):
        # Re-running init against an existing DB must not duplicate columns or error.
        store._init_db()
        cols = {row[1] for row in store._conn.execute("PRAGMA table_info(facts)").fetchall()}
        assert {"status", "superseded_by", "superseded_at"} <= cols

    def test_migrates_legacy_schema(self, tmp_path):
        # Simulate a pre-retraction DB (no status columns) and confirm init migrates it.
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            CREATE TABLE facts (
                fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                content         TEXT NOT NULL UNIQUE,
                category        TEXT DEFAULT 'general',
                tags            TEXT DEFAULT '',
                trust_score     REAL DEFAULT 0.5,
                retrieval_count INTEGER DEFAULT 0,
                helpful_count   INTEGER DEFAULT 0,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hrr_vector      BLOB
            )
            """
        )
        conn.execute("INSERT INTO facts (content, category) VALUES ('legacy fact', 'general')")
        conn.commit()
        conn.close()

        store = MemoryStore(db_path=str(db))
        cols = {row[1] for row in store._conn.execute("PRAGMA table_info(facts)").fetchall()}
        assert {"status", "superseded_by", "superseded_at"} <= cols
        rows = store.list_facts(include_inactive=True)
        assert len(rows) == 1
        assert rows[0]["content"] == "legacy fact"
        assert rows[0]["status"] == "active"


class TestSuppression:
    def _seed_chain(self, store):
        v1 = store.add_fact("Service Account Key policy: rotate every 90 days", category="general")
        v2 = store.add_fact("Service Account Key policy: rotate every 30 days", category="general")
        v3 = store.add_fact("Service Account Key policy: rotate every 14 days", category="general")
        store.supersede_fact(v1, v2)
        store.supersede_fact(v2, v3)
        return v1, v2, v3

    def test_search_excludes_superseded(self, store):
        v1, v2, v3 = self._seed_chain(store)
        results = store.search_facts("Service Account Key policy")
        ids = _fact_ids(results)
        assert v3 in ids
        assert v1 not in ids
        assert v2 not in ids

    def test_list_excludes_inactive_by_default(self, store):
        v1, v2, v3 = self._seed_chain(store)
        ids = _fact_ids(store.list_facts())
        assert v3 in ids
        assert v1 not in ids
        assert v2 not in ids

    def test_list_include_inactive_returns_all(self, store):
        v1, v2, v3 = self._seed_chain(store)
        ids = _fact_ids(store.list_facts(include_inactive=True))
        assert {v1, v2, v3} <= ids
        # rows carry status so the operator can distinguish states
        statuses = {r["fact_id"]: r["status"] for r in store.list_facts(include_inactive=True)}
        assert statuses[v1] == "superseded"
        assert statuses[v2] == "superseded"
        assert statuses[v3] == "active"

    def test_retriever_search_excludes_superseded(self, store, retriever):
        v1, v2, v3 = self._seed_chain(store)
        ids = _fact_ids(retriever.search("Service Account Key policy"))
        assert v3 in ids
        assert v1 not in ids
        assert v2 not in ids

    def test_probe_excludes_superseded(self, store, retriever):
        v1, v2, v3 = self._seed_chain(store)
        ids = _fact_ids(retriever.probe("Service Account Key"))
        assert v1 not in ids
        assert v2 not in ids

    def test_probe_with_category_excludes_superseded(self, store, retriever):
        v1, v2, v3 = self._seed_chain(store)
        ids = _fact_ids(retriever.probe("Service Account Key", category="general"))
        assert v1 not in ids
        assert v2 not in ids

    def test_related_excludes_superseded(self, store, retriever):
        v1, v2, v3 = self._seed_chain(store)
        ids = _fact_ids(retriever.related("Service Account Key"))
        assert v1 not in ids
        assert v2 not in ids

    def test_reason_excludes_superseded(self, store, retriever):
        v1, v2, v3 = self._seed_chain(store)
        ids = _fact_ids(retriever.reason(["Service Account Key"]))
        assert v1 not in ids
        assert v2 not in ids

    def test_contradict_scopes_to_active(self, store, retriever):
        v1, v2, v3 = self._seed_chain(store)
        pairs = retriever.contradict()
        superseded = {v1, v2}
        for pair in pairs:
            assert pair["fact_a"]["fact_id"] not in superseded
            assert pair["fact_b"]["fact_id"] not in superseded

    def test_restore_resurfaces_fact(self, store):
        v1, v2, v3 = self._seed_chain(store)
        store.restore_fact(v1)
        ids = _fact_ids(store.search_facts("Service Account Key policy"))
        assert v1 in ids


class TestToolHandler:
    def _provider(self, store):
        from plugins.memory.hermes_memory import HermesMemoryProvider

        p = HermesMemoryProvider(config={"db_path": str(store.db_path)})
        p.initialize("retraction-test-session")
        return p

    def test_retract_via_handler(self, store):
        p = self._provider(store)
        fid = store.add_fact("Service Account Key rotates every 90 days", category="general")
        data = json.loads(p.handle_tool_call("fact_store", {"action": "retract", "fact_id": fid}))
        assert data["status"] == "retracted"

    def test_supersede_via_handler(self, store):
        p = self._provider(store)
        a = store.add_fact("Service Account Key rotates every 90 days", category="general")
        b = store.add_fact("Service Account Key rotates every 30 days", category="general")
        data = json.loads(
            p.handle_tool_call("fact_store", {"action": "supersede", "fact_id": a, "by_fact_id": b})
        )
        assert data["status"] == "superseded"
        assert data["superseded_by"] == b

    def test_restore_via_handler(self, store):
        p = self._provider(store)
        fid = store.add_fact("Service Account Key rotates every 90 days", category="general")
        p.handle_tool_call("fact_store", {"action": "retract", "fact_id": fid})
        data = json.loads(p.handle_tool_call("fact_store", {"action": "restore", "fact_id": fid}))
        assert data["status"] == "active"

    def test_add_with_supersedes_via_handler(self, store):
        p = self._provider(store)
        old = store.add_fact("Service Account Key rotates every 90 days", category="general")
        data = json.loads(
            p.handle_tool_call(
                "fact_store",
                {"action": "add", "content": "Service Account Key rotates every 30 days", "supersedes": old},
            )
        )
        assert data["status"] == "added"
        new = data["fact_id"]
        old_row = store._conn.execute(
            "SELECT status, superseded_by FROM facts WHERE fact_id = ?", (old,)
        ).fetchone()
        assert old_row["status"] == "superseded"
        assert old_row["superseded_by"] == new

    def test_new_actions_in_schema_enum(self, store):
        p = self._provider(store)
        schema = next(s for s in p.get_tool_schemas() if s["name"] == "fact_store")
        action_enum = schema["parameters"]["properties"]["action"]["enum"]
        for action in ("retract", "supersede", "restore"):
            assert action in action_enum
