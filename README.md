# hermes-memory

A [Hermes](https://hermes-agent.nousresearch.com) memory plugin that gives
agents a structured, queryable fact store backed by SQLite. Built to solve two
problems: agents losing coherent working knowledge after context compaction,
and the need to convey context across sessions transparently — extending
factual recollection without hitting the cold tier and blowing out the context
window with large files.

## Architecture

The agent's memory has three tiers:

| Tier | Storage | What lives there | Fed by |
|---|---|---|---|
| **Cold** | OKF bundles on NFS (`/shared/agents/common/`) | Infrastructure facts — hosts, services, clusters, protocols. Curated, versioned, shared between agents. | Manual authoring + PR review |
| **Warm** | `memory_store.db` (this plugin) | Structured facts ingested from OKF + runtime learning. FTS5+Jaccard retrieval with trust scoring. | `okf_ingest.py` (cold→warm sync), agent's `fact_store` tool calls |
| **Hot** | Agent context window | Prefetched facts injected into the system prompt via `prefetch()`. Compaction recovery re-injects bootstrap infrastructure facts. | Plugin `prefetch()` + `on_session_switch` hook |

The cold tier is canonical. The warm tier is the working copy agents query at
runtime. The hot tier is what the agent actually sees. `okf_ingest.py` bridges
cold→warm; the plugin bridges warm→hot.

## How It Works

The plugin registers as a `MemoryProvider` and exposes two tools to the agent:

- **`fact_store`** — CRUD + retrieval over a structured fact store
- **`fact_feedback`** — rate a fact as helpful/unhelpful, adjusts trust score

Facts are stored in SQLite with:
- Full-text search (FTS5) for keyword retrieval
- Entity extraction and resolution (facts are linked to the entities they mention)
- Trust scores that drift up/down based on feedback

Retrieval is FTS5 + Jaccard similarity. The `probe`/`related`/`reason`/`contradict`
actions all operate over the same FTS5+Jaccard pipeline (`probe`/`related` search
by entity name, `reason` joins entity names into a keyword query, `contradict`
uses Jaccard entity overlap + Jaccard content divergence).

On every turn, `prefetch()` runs hybrid retrieval (FTS5 + Jaccard) against the
user's message and injects relevant facts into the context block.

### Compaction Recovery

When Hermes compresses context, the plugin detects the `compression`
session-switch event and re-injects the top-N high-trust `infrastructure`
category facts into the start of the next system prompt. This bootstraps the
agent back to knowing where it lives and what it manages — the host it runs on,
the clusters it monitors, the protocols it follows — without manual
intervention.

### Cross-Channel Awareness

Facts tagged with `source_session:` are treated as latent context from other
concurrent sessions. These surface in `prefetch()` under a separate "Other
Active Conversations" block and are suppressed if the current venue is more
public than the session they came from.

## OKF Ingestion

[OKF bundles](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)
are directories of YAML-frontmatter markdown files describing infrastructure
concepts — hosts, clusters, services, protocols. The cold tier lives on NFS at
`/shared/agents/common/`. `okf_ingest.py` walks a bundle and upserts facts into
the warm store.

```bash
python3 src/okf_ingest.py /shared/agents/common/infrastructure/

# dry run
python3 src/okf_ingest.py /shared/agents/common/infrastructure/ --dry-run --verbose

# explicit db path
python3 src/okf_ingest.py /path/to/bundle --db /path/to/memory_store.db
```

Ingestion is idempotent — files are tracked by path+timestamp and skipped if
unchanged. Trust is assigned from OKF v0.2 frontmatter signals:

| Trust | Source |
|---|---|
| 0.9 | `verified.by: human:...` or `bootstrap: true` |
| 0.7 | `verified.by: agent:...` |
| 0.5 | No verified block |

Deprecated files (`status: deprecated`) are purged from the store on next
ingest. Stale files (`stale_after` in the past) are skipped and purged if
previously ingested. Orphaned state rows (fact deleted but `okf_ingestion_state`
row survived) are detected and repaired by re-ingesting instead of skipping
([romar#19](https://github.com/sackheads/romar/issues/19)).

## Cross-Agent Fact Broadcast (NATS)

Facts published by one agent are automatically ingested by the other via a
write-side/poll-side pipeline over the NATS JetStream `agent-memory` stream
(subject `agents.memory.shared.>`):

```
agent A                    NATS JetStream               agent B
  nats-publish ──→  agent-memory stream  ──→  nats-listener (memory_poll_loop)
                                                    │
                                                    ├─ dedup (content hash)
                                                    ├─ normalize
                                                    └─ append to memory_spool.jsonl
                                                          │
                                              pre_llm_call hook (handler.py)
                                                          │
                                                    └─ upsert into memory_store.db
```

The listener polls the stream every 10 seconds alongside the coordination poll
loop.  Each fact is deduplicated by content hash before write, and the spool
file auto-trims oldest 25% when it exceeds 10MB.  The `pre_llm_call` hook reads
the spool and upserts into the warm store before every turn.

Trust scores work the same as OKF ingestion — `0.7` for infrastructure facts,
`0.9` for bootstrap — and land below the default `min_trust_threshold` (0.3) so
they're eligible for retrieval immediately.  The hot tier picks them up on the
next `prefetch()`.

This is the runtime path for shared knowledge.  OKF bundles handle curated,
versioned facts; the broadcast pipeline handles facts discovered at runtime and
pushed peer-to-peer.

## Installation

Drop the `src/plugins/memory/hermes_memory/` directory into your Hermes plugins
path and register it in `config.yaml` by its module name:

```yaml
plugins:
  hermes-memory-store:
    db_path: $HERMES_HOME/memory_store.db
    auto_extract: false
    default_trust: 0.5
    min_trust_threshold: 0.3
    bootstrap_inject_limit: 15
    bootstrap_min_trust: 0.7
    bootstrap_shadow: false
    okf_bundle_path: /shared/agents/common/infrastructure/
```

The plugin key `hermes-memory-store` matches the module's registration name
(`__init__.py` → `register()` → provider name `hermes-memory`). The directory
name `hermes_memory` is the Python module path.

## The `fact_store` Tool

| Action | Purpose |
|---|---|
| `add` | Store a fact. Requires `content`. Optional `category`, `tags`. |
| `search` | Keyword lookup across content and tags. |
| `probe` | All facts about a specific entity (keyword search on the entity name). |
| `related` | Facts connected to an entity (keyword search on the entity name). |
| `reason` | Facts matching a list of entities — joins entity names into a keyword query. |
| `contradict` | Find fact pairs making conflicting claims about the same entities (Jaccard entity overlap + content divergence). |
| `update` | Modify content, tags, category, or adjust trust by delta. |
| `remove` | Delete a fact by ID. |
| `list` | Browse facts by category/trust, sorted by trust descending. |

Categories: `user_pref`, `project`, `tool`, `general`, `infrastructure`.

```
# What do we know about the rune host?
fact_store(action="probe", entity="rune")

# Who works on backend and what do they do?
fact_store(action="reason", entities=["peppi", "backend"])

# Find anything about deploy processes
fact_store(action="search", query="deploy process")
```

## Configuration Reference

| Key | Default | Description |
|---|---|---|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite path. Supports `$HERMES_HOME` and `~` expansion. |
| `auto_extract` | `false` | Auto-extract facts from conversation at session end. |
| `default_trust` | `0.5` | Starting trust score for new facts. |
| `min_trust_threshold` | `0.3` | Prefetch ignores facts below this score. |
| `okf_bundle_path` | `/shared/agents/common/infrastructure/` | Default bundle for OKF ingestion. |
| `bootstrap_inject_limit` | `15` | Max facts re-injected after compaction. |
| `bootstrap_min_trust` | `0.7` | Minimum trust for compaction re-injection. |
| `bootstrap_shadow` | `false` | Log what would inject without injecting. |

## Project Structure

```
src/
  okf_ingest.py                              # OKF parser + ingestor (cold→warm)
  plugins/memory/
    __init__.py                              # Plugin registry
    hermes_memory/
      __init__.py                            # MemoryProvider plugin + fact_store tool
      store.py                               # SQLite schema, CRUD, entity resolution
      retrieval.py                           # FTS5 + Jaccard retrieval
tests/
  test_okf_ingest.py                         # 30 tests (parsing, trust, deprecation,
                                             #   staleness, orphan repair, CLI)
docs/
  problem.md                                 # Original design brief
  rune-review-2026-06-22.md                  # Architecture review
```

## Related

- [romar#19](https://github.com/sackheads/romar/issues/19) — orphaned OKF state
  rows permanently blocked re-ingestion (fixed)
- [romar#1](https://github.com/sackheads/romar/issues/1) — architecture paper
  audit that surfaced the dual-delivery-path design (listener + plugin)
- [Agent Inbox Pattern postmortem](/shared/agents/common/omgs/2026-08-08-nats-webhook-delivery-saga.md)
  — the `nats-listener.py` deliver→inbox path this plugin complements
