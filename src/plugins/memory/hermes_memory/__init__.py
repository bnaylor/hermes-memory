"""hermes-memory-store — hermes memory plugin using MemoryProvider interface.

Registers as a MemoryProvider plugin, giving the agent structured fact storage
with entity resolution and trust scoring.

Original plugin by dusterbloom (PR #2351), adapted to the MemoryProvider ABC.

Config in $HERMES_HOME/config.yaml (profile-scoped):
  plugins:
    hermes-memory-store:
      db_path: $HERMES_HOME/memory_store.db   # omit to use the default
      auto_extract: false
      default_trust: 0.5
      min_trust_threshold: 0.3
      temporal_decay_half_life: 0
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from .store import MemoryStore
from .retrieval import FactRetriever
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)


# Shadow-retrieval (opportunity) probe knobs. The master switch is the
# ``shadow_retrieval`` config flag (default off); these tune the probe when it
# is on. Kept as constants rather than config to keep the config surface small
# — they are measurement tuning, not behaviour a user toggles.
_SHADOW_MIN_TRUST = 0.5       # trust floor for candidate facts
_SHADOW_TOP_K = 5             # probe breadth (FTS candidates examined)
_SHADOW_JACCARD_THRESHOLD = 0.2  # min token overlap to count as an "opportunity"



# ---------------------------------------------------------------------------
# Tool schemas (unchanged from original PR)
# ---------------------------------------------------------------------------

FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory with algebraic reasoning. "
        "Use alongside the memory tool — memory for always-on context, "
        "fact_store for deep recall and compositional queries.\n\n"
        "ACTIONS (simple → powerful):\n"
        "• add — Store a fact the user would expect you to remember.\n"
        "• search — Keyword lookup ('editor config', 'deploy process').\n"
        "• probe — Entity recall: ALL facts about a person/thing.\n"
        "• related — What connects to an entity? Structural adjacency.\n"
        "• reason — Compositional: facts connected to MULTIPLE entities simultaneously.\n"
        "• contradict — Memory hygiene: find facts making conflicting claims.\n"
        "• retract — Mark a fact no longer true (no replacement).\n"
        "• supersede — Mark a fact replaced by another (records lineage).\n"
        "• restore — Re-activate a retracted/superseded fact.\n"
        "• update/remove/list — CRUD operations.\n\n"
        "IMPORTANT: Before answering questions about the user, ALWAYS probe or reason first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "search", "probe", "related", "reason", "contradict", "retract", "supersede", "restore", "update", "remove", "list"],
            },
            "content": {"type": "string", "description": "Fact content (required for 'add')."},
            "query": {"type": "string", "description": "Search query (required for 'search')."},
            "entity": {"type": "string", "description": "Entity name for 'probe'/'related'."},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names for 'reason'."},
            "fact_id": {"type": "integer", "description": "Fact ID for 'update'/'remove'/'retract'/'supersede'/'restore'."},
            "by_fact_id": {"type": "integer", "description": "Replacing fact ID for 'supersede'."},
            "supersedes": {"type": "integer", "description": "Old fact ID to supersede on 'add'."},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general", "infrastructure"]},
            "tags": {"type": "string", "description": "Comma-separated tags."},
            "trust_delta": {"type": "number", "description": "Trust adjustment for 'update'."},
            "min_trust": {"type": "number", "description": "Minimum trust filter (default: 0.3)."},
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["action"],
    },
}

FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": (
        "Rate a fact after using it. Mark 'helpful' if accurate, 'unhelpful' if outdated. "
        "This trains the memory — good facts rise, bad facts sink."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["helpful", "unhelpful"]},
            "fact_id": {"type": "integer", "description": "The fact ID to rate."},
        },
        "required": ["action", "fact_id"],
    },
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_plugin_config() -> dict:
    from hermes_constants import get_hermes_home
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
        with open(config_path, encoding="utf-8-sig") as f:
            all_config = yaml.safe_load(f) or {}
        return cfg_get(all_config, "plugins", "hermes-memory-store", default={}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HermesMemoryProvider(MemoryProvider):
    """Hermes memory with structured facts, entity resolution, and trust scoring."""

    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._store = None
        self._retriever = None
        self._session_id = ""
        self._min_trust = float(self._config.get("min_trust_threshold", 0.3))
        self._post_compaction: bool = False
        self._post_compaction_parent: str = ""
        self._shadow_retrieval = bool(self._config.get("shadow_retrieval", False))
        # Per-session high-water mark of retrieval_attempts.attempt_id, used to
        # detect "did the agent retrieve this turn" without a time query.
        self._shadow_seen_attempt_id: Dict[str, int] = {}

    @property
    def name(self) -> str:
        return "hermes-memory"

    def is_available(self) -> bool:
        return True  # SQLite is always available

    def save_config(self, values, hermes_home):
        """Write config to config.yaml under plugins.hermes-memory-store."""
        from pathlib import Path
        config_path = Path(hermes_home) / "config.yaml"
        try:
            import yaml
            existing = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8-sig") as f:
                    existing = yaml.safe_load(f) or {}
            existing.setdefault("plugins", {})
            existing["plugins"]["hermes-memory-store"] = values
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(existing, f, default_flow_style=False)
        except Exception:
            pass

    def get_config_schema(self):
        from hermes_constants import display_hermes_home
        _default_db = f"{display_hermes_home()}/memory_store.db"
        return [
            {"key": "db_path", "description": "SQLite database path", "default": _default_db},
            {"key": "auto_extract", "description": "Auto-extract facts at session end", "default": "false", "choices": ["true", "false"]},
            {"key": "default_trust", "description": "Default trust score for new facts", "default": "0.5"},
            {"key": "okf_bundle_path",        "description": "OKF bundle path for infrastructure facts",              "default": "/shared/agents/common/infrastructure/"},
            {"key": "bootstrap_inject_limit", "description": "Max facts injected after context compression",          "default": "15"},
            {"key": "bootstrap_min_trust",    "description": "Minimum trust score for post-compaction injection",     "default": "0.7"},
            {"key": "bootstrap_shadow",       "description": "Log what would inject without injecting (shadow mode)", "default": "false", "choices": ["true", "false"]},
            {"key": "shadow_retrieval",       "description": "Log per-turn retrieval opportunities (non-injecting probe)", "default": "false", "choices": ["true", "false"]},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        _hermes_home = str(get_hermes_home())
        _default_db = _hermes_home + "/memory_store.db"
        db_path = self._config.get("db_path", _default_db)
        # Expand $HERMES_HOME in user-supplied paths so config values like
        # "$HERMES_HOME/memory_store.db" or "~/.hermes/memory_store.db" both
        # resolve to the active profile's directory.
        if isinstance(db_path, str):
            db_path = db_path.replace("$HERMES_HOME", _hermes_home)
            db_path = db_path.replace("${HERMES_HOME}", _hermes_home)
        default_trust = float(self._config.get("default_trust", 0.5))
        temporal_decay = int(self._config.get("temporal_decay_half_life", 0))

        self._store = MemoryStore(db_path=db_path, default_trust=default_trust)
        self._retriever = FactRetriever(
            store=self._store,
            temporal_decay_half_life=temporal_decay,
        )
        self._session_id = session_id

    def system_prompt_block(self) -> str:
        if not self._store:
            self._post_compaction = False
            return ""

        bootstrap_block = ""
        if self._post_compaction:
            self._post_compaction = False
            try:
                bootstrap_block = self._build_bootstrap_block()
            except Exception as exc:
                logger.debug("system_prompt_block: bootstrap build failed: %s", exc)

        try:
            total = self._store._conn.execute(
                "SELECT COUNT(*) FROM facts"
            ).fetchone()[0]
        except Exception:
            total = 0

        if total == 0:
            memory_block = (
                "# Fact Store Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, preferences, decisions.\n"
                "Use fact_feedback to rate facts after using them (trains trust scores)."
            )
        else:
            memory_block = (
                f"# Fact Store Memory\n"
                f"Active. {total} facts stored with entity resolution and trust scoring.\n"
                f"Use fact_store to search, probe entities, reason across entities, or add facts.\n"
                f"Use fact_feedback to rate facts after using them (trains trust scores)."
            )

        if bootstrap_block:
            return bootstrap_block + "\n\n" + memory_block
        return memory_block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._retriever or not query:
            return ""
        try:
            # Get extra candidates to allow sensitivity/checkpoint filtering
            results = self._retriever.search(query, min_trust=self._min_trust, limit=10)
            if not results:
                return ""

            # Derive current venue sensitivity from session_id
            # session_id = "agent:main:discord:dm:..." or "agent:main:discord:thread:..."
            is_dm = ":dm:" in session_id
            is_restricted = is_dm or ":restricted:" in session_id
            current_sensitivity = "restricted" if is_restricted else "public"

            # Separate checkpoint facts from regular facts
            checkpoint_facts = []
            regular_facts = []
            for r in results:
                tags = r.get("tags", "") or ""
                if "source_session:" in tags:
                    # Sensitivity filter: skip restricted→public
                    if current_sensitivity == "public" and "sensitivity:restricted" in tags:
                        logger.debug(
                            "CHECKPOINT:filtered restricted from public (session=%s)",
                            session_id,
                        )
                        continue
                    checkpoint_facts.append(r)
                else:
                    regular_facts.append(r)

            parts = []

            # Regular facts section (existing format)
            if regular_facts:
                reg_lines = []
                for r in regular_facts[:5]:
                    trust = r.get("trust_score", r.get("trust", 0))
                    reg_lines.append(f"- [{trust:.1f}] {r.get('content', '')}")
                reg_lines.insert(0, "## Fact Store Memory")
                parts.append("\n".join(reg_lines))

            # Checkpoint facts section — structured latent-context block
            if checkpoint_facts:
                cp_lines = [
                    "## Other Active Conversations (Latent Context)",
                    "",
                    (
                        "You were recently discussing topics that may relate to the "
                        "current conversation. This is provided for your awareness — "
                        "do not raise these unprompted or redirect the conversation "
                        "toward them. Draw on this context only if the user's current "
                        "message is genuinely related."
                    ),
                    "",
                ]
                for r in checkpoint_facts[:3]:
                    content = r.get("content", "").strip()
                    tags = (r.get("tags", "") or "")

                    # Extract channel name from tags
                    channel = ""
                    for tag_part in tags.split(","):
                        tag_part = tag_part.strip()
                        if tag_part.startswith("source:"):
                            channel = tag_part[7:].strip()
                            break

                    # Extract topics from tags
                    topics = []
                    for tag_part in tags.split(","):
                        tag_part = tag_part.strip()
                        if tag_part.startswith("topic:"):
                            topic = tag_part[6:].strip()
                            topic = topic.replace("\\n", " ").replace("\\", "")
                            if len(topic) > 60:
                                topic = topic[:57] + "..."
                            topics.append(topic)

                    # Build display line
                    tag_summary = ", ".join(topics[:3]) if topics else ""
                    if channel and content:
                        entry = f"  • {channel}"
                        if content:
                            # Clean: remove "discussed" prefix if content is just topics
                            cleaned = content
                            if cleaned.startswith("discussed "):
                                pass  # keep it, it's fine
                            if len(cleaned) > 100:
                                cleaned = cleaned[:97] + "..."
                            entstr = f" — {cleaned}"
                        elif tag_summary:
                            entstr = f" — {tag_summary}"
                        else:
                            entstr = ""
                        cp_lines.append(entry + entstr)
                    elif content:
                        cp_lines.append(f"  • {content[:100]}")

                    # Log shadow-mode detail for validation
                    logger.info(
                        "CHECKPOINT:INJECT shadow session=%s channel=%s "
                        "content=%.80s topics=%s",
                        session_id, channel, content, tag_summary,
                    )

                parts.append("\n".join(cp_lines))

            return "\n\n".join(parts) if parts else ""
        except Exception as e:
            logger.debug("Fact store prefetch failed: %s", e)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # The fact store persists explicit facts via tools, not auto-sync.
        # The on_session_end hook handles auto-extraction if configured.
        #
        # When shadow_retrieval is enabled, this hook doubles as the shadow
        # (opportunity) probe: measure — without injecting — whether this turn
        # had high-confidence fact overlap, and whether the agent actually
        # retrieved. Opt-in (default off) and best-effort; a probe failure must
        # never surface to the turn.
        if not self._shadow_retrieval:
            return
        if not self._store or not self._retriever:
            return
        try:
            self._shadow_probe_turn(user_content, session_id)
        except Exception as exc:
            logger.debug("shadow retrieval probe failed: %s", exc)

    def _shadow_probe_turn(self, user_content: str, session_id: str) -> None:
        """Run one bounded, non-injecting opportunity probe and log the result."""
        store = self._store
        retriever = self._retriever
        if store is None or retriever is None:
            return
        sid = session_id or self._session_id

        # Demand: a new retrieval action since the last sync_turn means the
        # agent invoked the tool this turn.
        prev = self._shadow_seen_attempt_id.get(sid, 0)
        cur = store.latest_attempt_id(sid)
        demand_satisfied = cur > prev
        self._shadow_seen_attempt_id[sid] = cur

        content = (user_content or "").strip()
        if content:
            matched_count, top_fact_id, top_jaccard, _ = retriever.shadow_probe(
                content,
                min_trust=_SHADOW_MIN_TRUST,
                limit=_SHADOW_TOP_K,
                jaccard_threshold=_SHADOW_JACCARD_THRESHOLD,
            )
        else:
            matched_count, top_fact_id, top_jaccard = 0, None, 0.0

        store.record_opportunity(
            session_id=sid,
            matched_count=matched_count,
            top_fact_id=top_fact_id,
            top_jaccard=top_jaccard,
            demand_satisfied=demand_satisfied,
        )

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        if kwargs.get("reason") == "compression":
            self._post_compaction = True
            self._post_compaction_parent = parent_session_id

    def _build_bootstrap_block(self) -> str:
        """Fetch top infrastructure facts and format as a Bootstrap Context block.

        Returns "" in shadow mode (facts are only logged) or when store is empty.
        """
        shadow = bool(self._config.get("bootstrap_shadow", False))
        limit = int(self._config.get("bootstrap_inject_limit", 15))
        min_trust = float(self._config.get("bootstrap_min_trust", 0.7))

        try:
            facts = self._store.list_facts(
                category="infrastructure", min_trust=min_trust, limit=limit
            )
        except Exception as exc:
            logger.debug("_build_bootstrap_block: list_facts failed: %s", exc)
            return ""

        if not facts:
            return ""

        if shadow:
            logger.info(
                "BOOTSTRAP:shadow post-compaction would inject %d infrastructure facts",
                len(facts),
            )
            for f in facts:
                logger.info(
                    "BOOTSTRAP:shadow [%.1f] %.80s",
                    f.get("trust_score", 0),
                    f.get("content", ""),
                )
            return ""

        lines = [
            "## Bootstrap Context (Post-Compaction Recovery)",
            "",
            "Context was just compressed. Key infrastructure facts re-injected:",
            "",
        ]
        for f in facts:
            trust = f.get("trust_score", 0)
            content = f.get("content", "")
            lines.append(f"- [{trust:.1f}] {content}")

        return "\n".join(lines)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "fact_store":
            return self._handle_fact_store(args)
        elif tool_name == "fact_feedback":
            return self._handle_fact_feedback(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._config.get("auto_extract", False):
            return
        if not self._store or not messages:
            return
        self._auto_extract_facts(messages)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes as facts."""
        if action == "add" and self._store and content:
            try:
                category = "user_pref" if target == "user" else "general"
                self._store.add_fact(content, category=category)
            except Exception as e:
                logger.debug("Fact store memory_write mirror failed: %s", e)

    def shutdown(self) -> None:
        self._store = None
        self._retriever = None

    # -- Tool handlers -------------------------------------------------------

    def _attempt_query(self, action: str, args: dict) -> str | None:
        """Best-effort query string for the retrieval-attempt log.

        Captures what was asked for retrieval actions; ``None`` for write
        actions (add/update/remove/…) where "query" has no meaning.
        """
        if action == "search":
            return args.get("query")
        if action in ("probe", "related"):
            return args.get("entity")
        if action == "reason":
            entities = args.get("entities") or []
            return " ".join(entities) if entities else None
        if action == "contradict":
            return args.get("category")
        return None

    def _handle_fact_store(self, args: dict) -> str:
        try:
            action = args["action"]
            store = self._store
            retriever = self._retriever

            # Record the invocation before dispatch (retrieval-attempt
            # telemetry) so "was the tool invoked" is measured independently of
            # "did any fact surface".
            if store is not None:
                store.record_attempt(
                    action,
                    self._attempt_query(action, args),
                    self._session_id,
                )

            if action == "add":
                fact_id = store.add_fact(
                    args["content"],
                    category=args.get("category", "general"),
                    tags=args.get("tags", ""),
                    supersedes=args.get("supersedes"),
                )
                return json.dumps({"fact_id": fact_id, "status": "added"})

            elif action == "search":
                results = retriever.search(
                    args["query"],
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", self._min_trust)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results), "searched": args["query"]})

            elif action == "probe":
                results = retriever.probe(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results), "probed": args["entity"]})

            elif action == "related":
                results = retriever.related(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results), "related_to": args["entity"]})

            elif action == "reason":
                entities = args.get("entities", [])
                if not entities:
                    return tool_error("reason requires 'entities' list")
                results = retriever.reason(
                    entities,
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results), "reasoning_from": args["entities"]})

            elif action == "contradict":
                results = retriever.contradict(
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results), "contradiction_check": f"category={args.get('category', 'all')}"})

            elif action == "update":
                updated = store.update_fact(
                    int(args["fact_id"]),
                    content=args.get("content"),
                    trust_delta=float(args["trust_delta"]) if "trust_delta" in args else None,
                    tags=args.get("tags"),
                    category=args.get("category"),
                )
                return json.dumps({"updated": updated})

            elif action == "remove":
                removed = store.remove_fact(int(args["fact_id"]))
                return json.dumps({"removed": removed})

            elif action == "retract":
                result = store.retract_fact(int(args["fact_id"]))
                return json.dumps(result)

            elif action == "supersede":
                result = store.supersede_fact(int(args["fact_id"]), int(args["by_fact_id"]))
                return json.dumps(result)

            elif action == "restore":
                result = store.restore_fact(int(args["fact_id"]))
                return json.dumps(result)

            elif action == "list":
                facts = store.list_facts(
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", 0.0)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"facts": facts, "count": len(facts)})

            else:
                return tool_error(f"Unknown action: {action}")

        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_fact_feedback(self, args: dict) -> str:
        try:
            fact_id = int(args["fact_id"])
            helpful = args["action"] == "helpful"
            result = self._store.record_feedback(fact_id, helpful=helpful)
            return json.dumps(result)
        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return tool_error(str(exc))

    # -- Auto-extraction (on_session_end) ------------------------------------

    def _auto_extract_facts(self, messages: list) -> None:
        _PREF_PATTERNS = [
            re.compile(r'\bI\s+(?:prefer|like|love|use|want|need)\s+(.+)', re.IGNORECASE),
            re.compile(r'\bmy\s+(?:favorite|preferred|default)\s+\w+\s+is\s+(.+)', re.IGNORECASE),
            re.compile(r'\bI\s+(?:always|never|usually)\s+(.+)', re.IGNORECASE),
        ]
        _DECISION_PATTERNS = [
            re.compile(r'\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)', re.IGNORECASE),
            re.compile(r'\bthe\s+project\s+(?:uses|needs|requires)\s+(.+)', re.IGNORECASE),
        ]

        extracted = 0
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str) or len(content) < 10:
                continue

            for pattern in _PREF_PATTERNS:
                if pattern.search(content):
                    try:
                        self._store.add_fact(content[:400], category="user_pref")
                        extracted += 1
                    except Exception:
                        pass
                    break

            for pattern in _DECISION_PATTERNS:
                if pattern.search(content):
                    try:
                        self._store.add_fact(content[:400], category="project")
                        extracted += 1
                    except Exception:
                        pass
                    break

        if extracted:
            logger.info("Auto-extracted %d facts from conversation", extracted)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the hermes-memory provider with the plugin system."""
    config = _load_plugin_config()
    provider = HermesMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
