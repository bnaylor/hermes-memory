# tests/test_hermes_memory_compaction.py
"""Tests for compaction recovery: infrastructure category, config, on_session_switch, bootstrap injection."""

import pytest
from pathlib import Path
from plugins.memory.hermes_memory import HermesMemoryProvider
from plugins.memory.hermes_memory.store import MemoryStore


@pytest.fixture
def tmp_store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "mem.db"))


@pytest.fixture
def provider(tmp_store):
    config = {
        "db_path": str(tmp_store.db_path),
        "bootstrap_inject_limit": 5,
        "bootstrap_min_trust": 0.7,
        "bootstrap_shadow": False,
    }
    p = HermesMemoryProvider(config=config)
    p.initialize("test-session-001")
    return p


class TestInfrastructureCategory:
    def test_infrastructure_accepted_by_fact_store(self, provider, tmp_store):
        """fact_store(action='add') with category='infrastructure' must not return an error."""
        import json
        result = provider.handle_tool_call("fact_store", {
            "action": "add",
            "content": "test host: runs on hive.local",
            "category": "infrastructure",
        })
        data = json.loads(result)
        assert "fact_id" in data
        assert data.get("status") == "added"

    def test_infrastructure_in_schema_enum(self, provider):
        schemas = provider.get_tool_schemas()
        fact_store_schema = next(s for s in schemas if s["name"] == "fact_store")
        category_enum = fact_store_schema["parameters"]["properties"]["category"]["enum"]
        assert "infrastructure" in category_enum


class TestConfigKeys:
    def test_default_config_values(self):
        p = HermesMemoryProvider(config={})
        assert p._config.get("bootstrap_inject_limit", 15) == 15
        assert p._config.get("bootstrap_min_trust", 0.7) == 0.7
        assert p._config.get("bootstrap_shadow", False) == False
        assert p._config.get("okf_bundle_path", "/shared/agents/common/infrastructure/") == "/shared/agents/common/infrastructure/"

    def test_config_keys_in_schema(self):
        p = HermesMemoryProvider(config={})
        schema = p.get_config_schema()
        keys = {entry["key"] for entry in schema}
        assert "okf_bundle_path" in keys
        assert "bootstrap_inject_limit" in keys
        assert "bootstrap_min_trust" in keys
        assert "bootstrap_shadow" in keys


class TestCompactionHook:
    def test_compression_reason_sets_flag(self, provider):
        assert provider._post_compaction is False
        provider.on_session_switch(
            "new-session-002",
            parent_session_id="test-session-001",
            reason="compression",
        )
        assert provider._post_compaction is True

    def test_non_compression_reason_does_not_set_flag(self, provider):
        provider.on_session_switch("new-session-002", reason="resume")
        assert provider._post_compaction is False

    def test_no_reason_does_not_set_flag(self, provider):
        provider.on_session_switch("new-session-002")
        assert provider._post_compaction is False


class TestBootstrapInjection:
    def _seed_infra_facts(self, store):
        fid1 = store.add_fact(
            "rune-host: Runs on hive.local, Proxmox VM.",
            category="infrastructure",
            tags="bootstrap, type:Host",
        )
        store.update_fact(fid1, trust_delta=0.4)   # → 0.9
        fid2 = store.add_fact(
            "prod-cluster: Production k8s on romar.",
            category="infrastructure",
            tags="type:Cluster",
        )
        store.update_fact(fid2, trust_delta=0.2)   # → 0.7
        return fid1, fid2

    def test_bootstrap_block_appears_after_compression(self, provider, tmp_store):
        self._seed_infra_facts(tmp_store)
        provider.on_session_switch("new-session-002", reason="compression")
        block = provider.system_prompt_block()
        assert "Bootstrap Context" in block
        assert "rune-host" in block
        assert "prod-cluster" in block

    def test_bootstrap_block_absent_without_compression(self, provider, tmp_store):
        self._seed_infra_facts(tmp_store)
        block = provider.system_prompt_block()
        assert "Bootstrap Context" not in block

    def test_flag_cleared_after_first_call(self, provider, tmp_store):
        self._seed_infra_facts(tmp_store)
        provider.on_session_switch("new-session-002", reason="compression")
        provider.system_prompt_block()  # consumes the flag
        assert provider._post_compaction is False
        block2 = provider.system_prompt_block()
        assert "Bootstrap Context" not in block2

    def test_empty_store_produces_no_bootstrap_block(self, provider):
        provider.on_session_switch("new-session-002", reason="compression")
        block = provider.system_prompt_block()
        assert "Bootstrap Context" not in block

    def test_shadow_mode_logs_but_does_not_inject(self, tmp_store, caplog):
        import logging
        config = {
            "db_path": str(tmp_store.db_path),
            "bootstrap_shadow": True,
            "bootstrap_inject_limit": 5,
            "bootstrap_min_trust": 0.7,
        }
        p = HermesMemoryProvider(config=config)
        p.initialize("shadow-session")
        fid = tmp_store.add_fact(
            "rune-host: Runs on hive.local.", category="infrastructure", tags="bootstrap"
        )
        tmp_store.update_fact(fid, trust_delta=0.4)
        p.on_session_switch("shadow-session-002", reason="compression")
        with caplog.at_level(logging.INFO, logger="plugins.memory.hermes_memory"):
            block = p.system_prompt_block()
        assert "Bootstrap Context" not in block
        assert any("BOOTSTRAP:shadow" in r.message for r in caplog.records)

    def test_inject_limit_respected(self, provider, tmp_store):
        config = {
            "db_path": str(tmp_store.db_path),
            "bootstrap_inject_limit": 2,
            "bootstrap_min_trust": 0.7,
            "bootstrap_shadow": False,
        }
        p = HermesMemoryProvider(config=config)
        p.initialize("limit-session")
        for i in range(5):
            fid = tmp_store.add_fact(
                f"fact-{i}: Infrastructure fact number {i}.",
                category="infrastructure",
                tags="bootstrap",
            )
            tmp_store.update_fact(fid, trust_delta=0.4)
        p.on_session_switch("limit-session-002", reason="compression")
        block = p.system_prompt_block()
        fact_lines = [l for l in block.splitlines() if l.strip().startswith("-")]
        assert len(fact_lines) <= 2

    def test_flag_cleared_when_store_not_initialized(self):
        p = HermesMemoryProvider(config={})
        # Set the flag without calling initialize() — store is None
        p._post_compaction = True
        block = p.system_prompt_block()
        assert block == ""
        assert p._post_compaction is False
