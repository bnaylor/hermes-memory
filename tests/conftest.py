# tests/conftest.py
import sys
import types
from pathlib import Path

# --- Mock Hermes imports so MemoryStore is importable without a Hermes install ---

_hermes_constants = types.ModuleType("hermes_constants")
_hermes_constants.get_hermes_home = lambda: Path("/tmp/hermes_test_home")
_hermes_constants.display_hermes_home = lambda: "/tmp/hermes_test_home"
sys.modules["hermes_constants"] = _hermes_constants

_hermes_state = types.ModuleType("hermes_state")
_hermes_state.apply_wal_with_fallback = lambda conn, db_label="": None
sys.modules["hermes_state"] = _hermes_state

_hermes_cli = types.ModuleType("hermes_cli")
_hermes_cli_config = types.ModuleType("hermes_cli.config")
_hermes_cli_config.cfg_get = lambda cfg, *keys, default=None: default
sys.modules["hermes_cli"] = _hermes_cli
sys.modules["hermes_cli.config"] = _hermes_cli_config

_agent = types.ModuleType("agent")
_agent_mp = types.ModuleType("agent.memory_provider")

class MemoryProvider:
    def initialize(self, session_id, **kwargs): pass
    def system_prompt_block(self): return ""
    def prefetch(self, query, *, session_id=""): return ""
    def sync_turn(self, user_content, assistant_content, *, session_id=""): pass
    def get_tool_schemas(self): return []
    def handle_tool_call(self, tool_name, args, **kwargs): return ""
    def on_session_end(self, messages): pass
    def on_memory_write(self, action, target, content): pass
    def on_session_switch(self, new_session_id, **kwargs): pass
    def shutdown(self): pass
    def is_available(self): return True
    def save_config(self, values, hermes_home): pass
    def get_config_schema(self): return []
    @property
    def name(self): return "base"

_agent_mp.MemoryProvider = MemoryProvider
sys.modules["agent"] = _agent
sys.modules["agent.memory_provider"] = _agent_mp

_tools = types.ModuleType("tools")
_tools_registry = types.ModuleType("tools.registry")
_tools_registry.tool_error = lambda msg: f'{{"error": "{msg}"}}'
sys.modules["tools"] = _tools
sys.modules["tools.registry"] = _tools_registry

# Add project src/ and the hermes-memory plugin tree to sys.path
_here = Path(__file__).parent
sys.path.insert(0, str(_here.parent / "src"))
