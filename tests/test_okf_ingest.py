# tests/test_okf_ingest.py
import textwrap
from pathlib import Path
import pytest
from okf_ingest import parse_okf_concept, ParsedConcept, _HUMAN_TRUST, _AGENT_TRUST, _DEFAULT_TRUST


@pytest.fixture
def bundle(tmp_path):
    """Return a bundle root directory."""
    return tmp_path


def write_concept(bundle: Path, rel_path: str, content: str) -> Path:
    p = bundle / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


class TestParseOkfConcept:
    def test_full_frontmatter(self, bundle):
        f = write_concept(bundle, "hosts/rune.md", """\
            ---
            type: Host
            title: rune-host
            description: Rune runs on hive.local, a Proxmox VM.
            tags: [infrastructure, host]
            timestamp: 2026-06-22T00:00:00Z
            bootstrap: true
            ---
            Body text here.
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.content == "rune-host: Rune runs on hive.local, a Proxmox VM."
        assert result.category == "infrastructure"
        assert result.trust == _HUMAN_TRUST
        assert "bootstrap" in result.tags_str
        assert "source:okf" in result.tags_str
        assert "type:Host" in result.tags_str
        assert result.timestamp == "2026-06-22T00:00:00Z"
        assert result.file_path == "hosts/rune.md"

    def test_no_bootstrap_flag_and_no_verified_defaults_to_05(self, bundle):
        f = write_concept(bundle, "clusters/prod.md", """\
            ---
            type: Cluster
            title: prod-cluster
            description: Production k8s cluster on romar.
            tags: [k8s]
            timestamp: 2026-06-01T00:00:00Z
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.trust == _DEFAULT_TRUST
        assert "bootstrap" not in result.tags_str

    def test_verified_by_human_gives_09_trust(self, bundle):
        f = write_concept(bundle, "services/actual-budget.md", """\
            ---
            type: Service
            title: Actual Budget
            description: Personal finance with GoCardless sync.
            tags: [service, finance]
            verified:
              at: "2026-07-22"
              by: "human:scromp"
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.trust == _HUMAN_TRUST

    def test_verified_by_agent_gives_07_trust(self, bundle):
        f = write_concept(bundle, "services/nats.md", """\
            ---
            type: Service
            title: NATS
            description: Inter-agent messaging bus.
            tags: [service, messaging]
            verified:
              at: "2026-07-20"
              by: "agent:clomp"
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.trust == _AGENT_TRUST

    def test_no_verified_block_defaults_to_05(self, bundle):
        f = write_concept(bundle, "services/smokeping.md", """\
            ---
            type: Service
            title: SmokePing
            description: Latency monitoring.
            tags: [service, monitoring]
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.trust == _DEFAULT_TRUST

    def test_status_deprecated_returns_none(self, bundle):
        f = write_concept(bundle, "services/old-thing.md", """\
            ---
            type: Service
            title: Old Thing
            description: Deprecated service.
            status: deprecated
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_stale_after_past_returns_none(self, bundle):
        f = write_concept(bundle, "services/expired.md", """\
            ---
            type: Service
            title: Expired
            description: This is stale.
            stale_after: "2020-01-01"
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_stale_after_future_is_accepted(self, bundle):
        f = write_concept(bundle, "services/future.md", """\
            ---
            type: Service
            title: Future
            description: Not stale yet.
            stale_after: "2099-12-31"
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.trust == _DEFAULT_TRUST

    def test_missing_description_falls_back_to_body(self, bundle):
        f = write_concept(bundle, "hosts/clomp.md", """\
            ---
            type: Host
            title: clomp-host
            ---
            Clomp runs on nest.local with 4 cores.
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.content == "clomp-host: Clomp runs on nest.local with 4 cores."

    def test_heading_lines_skipped_in_body_fallback(self, bundle):
        f = write_concept(bundle, "hosts/clomp.md", """\
            ---
            type: Host
            title: clomp-host
            ---
            # Overview
            Clomp runs on nest.local with 4 cores.
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert "Overview" not in result.content
        assert "nest.local" in result.content

    def test_missing_type_returns_none(self, bundle):
        f = write_concept(bundle, "bad.md", """\
            ---
            title: No Type
            description: This has no type field.
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_no_frontmatter_returns_none(self, bundle):
        f = write_concept(bundle, "plain.md", "Just plain text, no frontmatter.\n")
        assert parse_okf_concept(f, bundle) is None

    def test_empty_description_and_empty_body_returns_none(self, bundle):
        f = write_concept(bundle, "empty.md", """\
            ---
            type: Host
            title: ghost
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_skip_index_md(self, bundle):
        f = write_concept(bundle, "index.md", """\
            ---
            type: Index
            title: Root Index
            description: Bundle root.
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_skip_log_md(self, bundle):
        f = write_concept(bundle, "log.md", """\
            ---
            type: Log
            title: Log
            description: Change log.
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_category_override(self, bundle):
        f = write_concept(bundle, "prefs/editor.md", """\
            ---
            type: Preference
            title: editor
            description: Uses neovim with LazyVim config.
            category: user_pref
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.category == "user_pref"

    def test_title_falls_back_to_stem(self, bundle):
        f = write_concept(bundle, "hosts/rune.md", """\
            ---
            type: Host
            description: Runs on hive.local.
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.content.startswith("rune: ")

    def test_tags_string_and_list_forms(self, bundle):
        f = write_concept(bundle, "c.md", """\
            ---
            type: Cluster
            title: c
            description: A cluster.
            tags: k8s, prod
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert "k8s" in result.tags_str
        assert "prod" in result.tags_str


import sqlite3
import textwrap
from plugins.memory.hermes_memory.store import MemoryStore


@pytest.fixture
def tmp_db(tmp_path):
    """Return a MemoryStore backed by a temp SQLite file."""
    db = tmp_path / "test_memory.db"
    return MemoryStore(db_path=str(db))


@pytest.fixture
def bundle_with_facts(bundle):
    """Bundle with two concepts — one bootstrap, one standard."""
    write_concept(bundle, "hosts/rune.md", """\
        ---
        type: Host
        title: rune-host
        description: Runs on hive.local, Proxmox VM.
        tags: [host]
        timestamp: 2026-06-22T00:00:00Z
        bootstrap: true
        ---
    """)
    write_concept(bundle, "clusters/prod.md", """\
        ---
        type: Cluster
        title: prod-cluster
        description: Production k8s on romar.
        tags: [k8s]
        timestamp: 2026-06-01T00:00:00Z
        ---
    """)
    return bundle


class TestOKFIngestor:
    def test_ingest_adds_facts(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(bundle_with_facts)
        assert result.added == 2
        assert result.updated == 0
        assert result.skipped == 0
        assert result.errors == []

    def test_facts_have_infrastructure_category(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        facts = tmp_db.list_facts(category="infrastructure")
        assert len(facts) == 2

    def test_bootstrap_fact_has_high_trust(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor, _HUMAN_TRUST
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        facts = tmp_db.list_facts(category="infrastructure", min_trust=0.85)
        assert len(facts) == 1
        assert abs(facts[0]["trust_score"] - _HUMAN_TRUST) < 0.01

    def test_second_run_skips_unchanged(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        result2 = ingestor.ingest_bundle(bundle_with_facts)
        assert result2.added == 0
        assert result2.skipped == 2

    def test_changed_timestamp_triggers_update(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        # Overwrite one file with a new timestamp
        write_concept(bundle_with_facts, "hosts/rune.md", """\
            ---
            type: Host
            title: rune-host
            description: Now runs on hive2.local after migration.
            tags: [host]
            timestamp: 2026-06-23T00:00:00Z
            bootstrap: true
            ---
        """)
        result2 = ingestor.ingest_bundle(bundle_with_facts)
        assert result2.updated == 1
        assert result2.skipped == 1
        # Verify content was updated
        facts = tmp_db.list_facts(category="infrastructure")
        contents = {f["content"] for f in facts}
        assert any("hive2.local" in c for c in contents)

    def test_dry_run_does_not_write(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(bundle_with_facts, dry_run=True)
        assert result.added == 2      # counted but not written
        facts = tmp_db.list_facts(category="infrastructure")
        assert len(facts) == 0        # nothing actually stored

    def test_missing_bundle_path_returns_error(self, tmp_db, tmp_path):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(tmp_path / "nonexistent")
        assert len(result.errors) == 1
        assert result.added == 0

    def test_skips_index_and_log(self, tmp_db, bundle):
        from okf_ingest import OKFIngestor
        write_concept(bundle, "index.md", "---\ntype: Index\ntitle: i\ndescription: d\n---\n")
        write_concept(bundle, "log.md", "---\ntype: Log\ntitle: l\ndescription: d\n---\n")
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(bundle)
        assert result.added == 0
        assert result.skipped == 0  # skipped filenames aren't counted as skipped

    def test_deprecated_file_gets_purged(self, tmp_db, bundle):
        """Verify that a previously-ingested file marked deprecated is purged."""
        from okf_ingest import OKFIngestor

        # Ingest a valid file first
        write_concept(bundle, "hosts/rune.md", """\
            ---
            type: Host
            title: rune-host
            description: Runs on hive.local, Proxmox VM.
            tags: [host]
            bootstrap: true
            ---
        """)
        ingestor = OKFIngestor(tmp_db)
        result1 = ingestor.ingest_bundle(bundle)
        assert result1.added == 1

        # Now mark it deprecated
        write_concept(bundle, "hosts/rune.md", """\
            ---
            type: Host
            title: rune-host
            description: Runs on hive.local, Proxmox VM.
            tags: [host]
            status: deprecated
            ---
        """)
        result2 = ingestor.ingest_bundle(bundle)
        assert result2.purged == 1
        assert result2.added == 0

        # Verify fact was removed from store
        facts = tmp_db.list_facts(category="infrastructure")
        assert len(facts) == 0

    def test_orphaned_state_row_reingested(self, tmp_db, bundle):
        """When a fact is deleted but its state row survives, re-ingest adds it back."""
        from okf_ingest import OKFIngestor

        # Ingest a fact normally
        write_concept(bundle, "hosts/rune.md", """\
            ---
            type: Host
            title: rune-host
            description: Runs on hive.local, Proxmox VM.
            timestamp: 2026-06-22T00:00:00Z
            tags: [host]
            ---
        """)
        ingestor = OKFIngestor(tmp_db)
        result1 = ingestor.ingest_bundle(bundle)
        assert result1.added == 1

        # Simulate orphan: delete the fact but leave the state row
        tmp_db._conn.execute("DELETE FROM facts")
        tmp_db._conn.commit()
        facts_after_delete = tmp_db.list_facts(category="infrastructure")
        assert len(facts_after_delete) == 0

        # Verify state row still exists
        state_rows = tmp_db._conn.execute(
            "SELECT COUNT(*) FROM okf_ingestion_state"
        ).fetchone()[0]
        assert state_rows == 1, "state row should survive fact deletion"

        # Re-ingest the same file — should add (not skip), repairing the orphan
        result2 = ingestor.ingest_bundle(bundle)
        assert result2.added == 1, f"expected added=1 (repaired orphan), got {result2}"
        assert result2.skipped == 0

        # Verify fact is back
        facts = tmp_db.list_facts(category="infrastructure")
        assert len(facts) == 1


import subprocess
import sys


class TestCLI:
    def test_dry_run_prints_summary(self, bundle_with_facts, tmp_path):
        db = tmp_path / "cli_test.db"
        result = subprocess.run(
            [sys.executable, "-m", "okf_ingest",
             str(bundle_with_facts), "--db", str(db), "--dry-run"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent / "src"),
        )
        assert result.returncode == 0
        assert "added=2" in result.stdout

    def test_missing_bundle_exits_nonzero(self, tmp_path):
        db = tmp_path / "cli_test.db"
        result = subprocess.run(
            [sys.executable, "-m", "okf_ingest",
             str(tmp_path / "no_such_dir"), "--db", str(db)],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent / "src"),
        )
        assert result.returncode != 0
