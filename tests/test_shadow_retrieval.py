# tests/test_shadow_retrieval.py
"""Tests for the shadow-retrieval (opportunity) instrument (romar#318).

The shadow probe measures, per turn, whether the fact store *could have* helped
(high-confidence fact overlap) and whether the agent actually retrieved. The
invariant under test: the probe is non-injecting — it must never increment
``retrieval_count`` (the surfacings metric it is measured against) — and it is
opt-in (``shadow_retrieval`` defaults off).
"""
import pytest

from plugins.memory.hermes_memory.store import MemoryStore
from plugins.memory.hermes_memory.retrieval import FactRetriever
from plugins.memory.hermes_memory import HermesMemoryProvider


@pytest.fixture
def store(tmp_path):
    """A MemoryStore backed by a temp SQLite file."""
    return MemoryStore(db_path=str(tmp_path / "test_memory.db"))


@pytest.fixture
def retriever(store):
    return FactRetriever(store=store)


@pytest.fixture
def provider(tmp_path):
    """A HermesMemoryProvider with shadow_retrieval enabled and a temp DB."""
    p = HermesMemoryProvider(config={
        "db_path": str(tmp_path / "mem.db"),
        "shadow_retrieval": True,
    })
    p.initialize("sess-1")
    return p


class TestShadowProbe:
    def test_match_returns_fact_and_does_not_increment_retrieval_count(self, store, retriever):
        fid = store.add_fact("nats is the coordination bus", category="infrastructure")
        matched, top_id, top_jaccard, matches = retriever.shadow_probe(
            "nats coordination bus", min_trust=0.5, limit=5, jaccard_threshold=0.2,
        )
        assert matched == 1
        assert top_id == fid
        assert top_jaccard >= 0.2
        assert len(matches) == 1
        # Non-injecting: the probe must NOT bump the surfacings counter.
        facts = store.list_facts()
        assert facts[0]["retrieval_count"] == 0

    def test_no_match_returns_empty(self, store, retriever):
        store.add_fact("nats is the coordination bus", category="infrastructure")
        matched, top_id, top_jaccard, matches = retriever.shadow_probe(
            "completely unrelated cooking topic", min_trust=0.5, limit=5,
            jaccard_threshold=0.2,
        )
        assert matched == 0
        assert top_id is None
        assert top_jaccard == 0.0
        assert matches == []

    def test_below_jaccard_threshold_is_not_a_match(self, store, retriever):
        store.add_fact("nats is the coordination bus", category="infrastructure")
        # High threshold excludes the overlap.
        matched, top_id, top_jaccard, _ = retriever.shadow_probe(
            "nats coordination bus", min_trust=0.5, limit=5, jaccard_threshold=0.9,
        )
        assert matched == 0
        assert top_id is None
        assert top_jaccard == 0.0

    def test_below_trust_floor_is_not_a_candidate(self, store, retriever):
        store.add_fact("nats is the coordination bus", category="infrastructure")
        # Trust floor above the stored fact's trust (0.5) excludes it.
        matched, top_id, _, _ = retriever.shadow_probe(
            "nats coordination bus", min_trust=0.9, limit=5, jaccard_threshold=0.2,
        )
        assert matched == 0
        assert top_id is None


class TestRecordOpportunityAndDemand:
    def test_latest_attempt_id_only_counts_retrieval_actions(self, store):
        assert store.latest_attempt_id("sess-1") == 0
        store.record_attempt("add", None, "sess-1")          # write — not retrieval
        assert store.latest_attempt_id("sess-1") == 0
        store.record_attempt("search", "nats", "sess-1")     # retrieval
        assert store.latest_attempt_id("sess-1") == 2
        store.record_attempt("probe", "nats", "sess-1")      # retrieval
        assert store.latest_attempt_id("sess-1") == 3

    def test_record_opportunity_writes_row(self, store):
        store.record_opportunity(
            session_id="sess-1", matched_count=2, top_fact_id=7,
            top_jaccard=0.6, demand_satisfied=True,
        )
        row = store._conn.execute(
            "SELECT * FROM retrieval_opportunities"
        ).fetchone()
        assert row["session_id"] == "sess-1"
        assert row["matched_count"] == 2
        assert row["top_fact_id"] == 7
        assert row["top_jaccard"] == 0.6
        assert row["demand_satisfied"] == 1

    def test_record_opportunity_is_best_effort(self, store):
        # Dropping the table must not raise — telemetry must never break the turn.
        store._conn.execute("DROP TABLE retrieval_opportunities")
        store._conn.commit()
        store.record_opportunity(session_id="sess-1", matched_count=1)


class TestSyncTurnHook:
    def test_blind_turn_logged_with_demand_zero(self, provider):
        provider._store.add_fact("nats is the coordination bus", category="infrastructure")
        provider.sync_turn("nats coordination bus", "", session_id="sess-1")
        rows = provider._store._conn.execute(
            "SELECT * FROM retrieval_opportunities"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["matched_count"] == 1
        assert rows[0]["demand_satisfied"] == 0   # no retrieval this turn → blind

    def test_satisfied_demand_logged(self, provider):
        provider._store.add_fact("nats is the coordination bus", category="infrastructure")
        # Simulate the agent invoking the retrieval tool this turn.
        provider._store.record_attempt("search", "nats", "sess-1")
        provider.sync_turn("nats coordination bus", "", session_id="sess-1")
        row = provider._store._conn.execute(
            "SELECT * FROM retrieval_opportunities"
        ).fetchone()
        assert row["demand_satisfied"] == 1

    def test_no_match_logs_zero(self, provider):
        provider.sync_turn("unrelated cooking topic", "", session_id="sess-1")
        row = provider._store._conn.execute(
            "SELECT * FROM retrieval_opportunities"
        ).fetchone()
        assert row["matched_count"] == 0
        assert row["top_fact_id"] is None

    def test_disabled_by_default(self, tmp_path):
        p = HermesMemoryProvider(config={"db_path": str(tmp_path / "mem.db")})
        assert p._shadow_retrieval is False
        p.initialize("sess-1")
        p._store.add_fact("nats is the coordination bus", category="infrastructure")
        p.sync_turn("nats coordination bus", "", session_id="sess-1")
        count = p._store._conn.execute(
            "SELECT COUNT(*) AS c FROM retrieval_opportunities"
        ).fetchone()["c"]
        assert count == 0
