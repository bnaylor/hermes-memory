# Memory Gap Closure Plan

**Type:** Implemented
**Status:** ✅ Implemented (2026-08-24) — all six phases merged
**Authors:** Rune & Clomp (co-authored)
**Trigger:** Cross-review of dshnayder/kube-agents Hindsight proposal (PR #634) + mastersingh24 review
**Date:** 2026-08-11 (drafted) · finalized 2026-08-24 by Clomp (per scromp directive)

> **Status note:** This document is retained as the design record. The six phases
> below are all shipped. See "Implementation status" and "Post-implementation
> findings" for what landed and what remains open (notably Gap #6 — durability).

## Implementation status

| Phase | Title | Status | Commit | PR |
|---|---|---|---|---|
| 0 | Rename plugin `holographic` → `hermes_memory` | ✅ Done | `987eb69` | #8 (romar#43) |
| 1 | "Read names its outcome" | ✅ Done | `7b8aa96` | #2 |
| 2 | Fix `retrieval_count` tracking | ✅ Done | `7b8aa96` | #2 |
| 3 | Fact store inventory script + cron | ✅ Done | `da6a763` | #3 |
| 4 | Spool pipeline sanity check | ✅ Done | `da6a763` | #3 |
| 5 | Skill content staleness check | ✅ Done | `c99ea2f` | #4 |

Both scripts are deployed and scheduled: `fact-store-inventory` (weekly Sunday
09:00, `no_agent`) and `skill-content-check` (monthly 1st 09:00, `no_agent`).

---

## Context

Three independent sources converged on the same gap: our memory subsystem has zero measurement and two known interface deficiencies.

1. **dshnayder/kube-agents Hindsight proposal** — a retrieval-backed memory design with per-rung A/B metrics (gold recall, contamination, ranking, context tokens). They measured theirs against a synthetic 1,664-record corpus. We have no equivalent data.

2. **mastersingh24 PR #634 review** — called out the proposal's own benchmark as insufficient (compared only to flat-file, not Honcho) and flagged the 882-line multi-tenancy wrapper as self-inflicted complexity. Reinforced that measurement without honest baselines is marketing.

3. **Our own fact_store** — `retrieval_count` is always 0 (the field exists in the schema but the hybrid retrieval path never increments it). We can't answer "how often is a fact retrieved?" or "which facts are dead weight?"

Additionally, two interface gaps were identified:

4. **"Read names its outcome"** — the Hindsight proposal identified a failure mode where models silently convert "not in what I retrieved" into "not recorded anywhere." Our fact_store has the same deficiency: empty results carry no trace of what was searched.

5. **Skill content staleness** — the Curator detects time-based staleness (idle hours) but not content staleness. A skill loaded daily that references a tool no longer installed goes undetected.

---

## Scope

This plan addresses the **measurement, interface, and staleness gaps** in our homelab memory stack. It does NOT address:

- Multi-tenant / per-user scoping (single-human homelab)
- Semantic dedup (deferred until facts cross 500)
- Hindsight-style LLM consolidation (their own experiment caused identifier collapse: 162→50 distinct IDs in 3 cycles)
- Token budget mechanisms (the `limit` parameter already exists; 10 is correct for our scale)

---

## Plan

### 0. Rename the plugin

**Issue:** The plugin directory is `holographic/` in both `~/.hermes/hermes-agent/plugins/memory/` and the source repo at `sackheads/hermes-memory`. The retrieval engine is FTS5 + Jaccard + optional HRR — "holographic" is misleading. Tracked as [romar#43](https://github.com/sackheads/romar/issues/43).

**Fix:** `s/holographic/hermes_memory/` in the plugin directory and `__init__.py` import paths. No behavior change. (Note: the directory must be the underscore form `hermes_memory` — a valid Python package name — see the naming gotcha under Open questions #3.)

**Effort:** 5 minutes. **Status:** ✅ Done — PR #8, commit `987eb69`.

---

### 1. "Read names its outcome" — include query in tool responses

**What:** When fact_store returns empty results, the response should say what was searched. This prevents the model from silently converting "not in my retrieval results" into "not recorded anywhere."

**Where:** `__init__.py` handler — `_handle_fact_store()`. All retrieval actions (search, probe, related, reason) return `{"results": [...], "count": N}` without naming what was searched.

**Fix:** Add the input parameters to every response body:

```python
# search
json.dumps({"results": results, "count": len(results), "searched": args["query"]})

# probe
json.dumps({"results": results, "count": len(results), "probed": args["entity"]})

# related
json.dumps({"results": results, "count": len(results), "related_to": args["entity"]})

# reason
json.dumps({"results": results, "count": len(results), "reasoning_from": args["entities"]})
```

When results are empty, the model sees `{"results": [], "count": 0, "searched": "nats config"}` instead of `{"results": [], "count": 0}`.

**Effort:** 10 minutes. **Status:** ✅ Done — PR #2, commit `7b8aa96`.

---

### 2. Fix `retrieval_count` tracking

**Bug:** The hybrid retrieval path in `retrieval.py` — `FactRetriever.search()`, `probe()`, `related()`, `reason()` — returns candidates but never increments `retrieval_count`. Only `store.search_facts()` (store.py:231) increments it, and nothing calls that path from the plugin tool handler. Every fact shows `retrieval_count: 0` regardless of actual usage.

**Fix:** Add a post-query increment to each retrieval method (`_increment_retrievals()`), mirroring the existing pattern in `store.search_facts()`:

```python
if results:
    ids = [r["fact_id"] for r in results]
    placeholders = ",".join("?" * len(ids))
    self.store._conn.execute(
        f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})",
        ids,
    )
    self.store._conn.commit()
```

**Why this is a dependency:** The inventory script (Phase 3) depends on accurate `retrieval_count` to identify cold facts. Without this fix, every fact appears dead regardless of actual retrieval frequency.

**Effort:** 15 minutes. **Status:** ✅ Done — PR #2, commit `7b8aa96`.

---

### 3. Fact store inventory script (weekly cron)

**What:** A `no_agent` cron job that queries `memory_store.db` directly and emits a human-readable weekly digest. No LLM — pure data dump.

**Path:** `~/.hermes/scripts/fact-store-inventory.py`

**Metrics emitted:**

| Metric | Why |
|---|---|
| Total facts, per category, per trust tier | Baseline. Are we growing? |
| `retrieval_count = 0` facts | Dead-fact detection — candidates for removal |
| Trust score distribution | Are we accumulating 0.5-trust drift? |
| Fact age percentiles (p50, p90, max) | Is the store aging or refreshing? |
| Facts added this week vs. last week | Growth rate |
| `⚠ dedup threshold: N/500 facts` | When to revisit the semantic dedup decision |

**Cron schedule:** Weekly, Sunday 9am EST, delivered to origin:

```
hermes cron create \
  --schedule "0 9 * * 0" \
  --name "fact-store-inventory" \
  --no-agent \
  --script fact-store-inventory.py
```

**DB access:** Read-only. Opens `~/.hermes/memory_store.db`, runs COUNT/GROUP BY queries, formats output.

**Effort:** 30 minutes. **Status:** ✅ Done — PR #3, commit `da6a763`. Cron live, first runs 2026-08-16 and 2026-08-23 (both `ok`).

---

### 4. Spool pipeline sanity check (free with #3)

**What:** The NATS → spool → ingest pipeline (`nats-listener.py` → `memory_spool.jsonl` → `ingest-memory-spool` hook → `memory_store.db`) is invisible. A companion check added to the same inventory script:

- Count spool lines (`wc -l ~/.hermes/memory_spool.jsonl`)
- Count DB rows
- Flag if spool has lines older than N hours (indicates the ingest hook isn't running)

**Deliverable:** Added to the weekly digest script for zero marginal effort.

**Status:** ✅ Done — PR #3, commit `da6a763`.

---

### 5. Skill content staleness (enhance the Curator)

**What's already good:** The Curator tracks `use_count`, `view_count`, `patch_count`, `last_used_at` per skill. Auto-transitions to `stale` based on `min_idle_hours`. Archives dead skills. Latest run: 4 marked stale, 3 reactivated, 100 checked.

**What's missing:** Time-based staleness ≠ content staleness. A skill loaded daily whose `required_commands` reference a tool no longer installed (e.g., `helm` after a migration) is time-active but content-dead. The `required_commands` and `required_environment_variables` fields already exist in skill frontmatter but are never verified.

**Fix:** Add an optional `--content-check` pass to the Curator's auto-transition phase. For each active skill:

1. Parse YAML frontmatter for `required_commands`
2. Check `which <cmd>` on the host
3. Flag skill as `content-stale` if any command is missing

**Output:** New line in Curator REPORT.md:

```
- content-stale (commands missing): 2 (microk8s-janitor: helm not found, garage-janitor: garage not found)
```

**Safety:** Content-stale is a separate flag from time-stale. Content-stale skills are flagged for review but not auto-archived (too aggressive). The Curator continues to handle time-based staleness independently.

**Effort:** ~1 hour. **Status:** ✅ Done — PR #4, commit `c99ea2f`. Note: shipped as a standalone `skill-content-check.py` script + monthly cron (not the `--content-check` Curator flag described above) — a deliberate simplification that avoids coupling to Curator internals.

---

## Execution order

| # | Phase | Value | Effort | Dependency |
|---|---|---|---|---|
| 0 | Rename plugin | Low (correctness) | 5min | None |
| 1 | Read names its outcome | Medium (correctness) | 10min | None |
| 2 | Fix retrieval_count | **High** (enables #3) | 15min | None |
| 3 | Inventory script + cron | **High** (visibility) | 30min | #2 (needs accurate counts) |
| 4 | Spool sanity check | Low (monitoring) | Free | #3 |
| 5 | Curator content checks | Medium (staleness) | ~1h | Curator infra |

**Total:** ~2 hours. Phases 0–2 are code changes in the hermes-memory plugin (one PR). Phase 3 is a standalone script. Phase 5 shipped as a standalone script (see above).

---

## What stays deferred

- **Semantic dedup** — hash-based dedup sufficient below 500 facts. Inventory script (#3) will flag when threshold approaches.
- **Hindsight-style LLM consolidation** — their own experiment caused identifier collapse (162→50 distinct IDs in 3 cycles). We're not replicating that failure.
- **Multi-tenant scoping** — single-human homelab. Not needed.
- **Recall budget / `max_tokens`** — `limit` parameter already exists; 10 is correct for our scale.
- **Out-of-band identifiers** — clean architecturally but no practical benefit at 120 facts with two agents.
- **HRR vector algebra** — removed entirely (PR #9, commit `95aff2a`/`d5d7e5e` merge), superseding the "optional HRR" description in the original Phase 0 rationale.

---

## Open questions — resolved

1. **Should `retrieval_count` track retrievals per fact as a histogram, or is a simple counter sufficient?**
   → **Simple counter shipped.** `_increment_retrievals()` on all four retrieval paths (PR #2). The inventory script's `retrieval_count = 0` (cold) vs `> 0` (warm) split is the right granularity at ~155 facts. Revisit a histogram if facts cross ~1000.

2. **Should the inventory script also detect fact contradictions?**
   → **Deferred.** `contradict` remains an on-demand retriever action. The retraction primitive (PR #7) now provides the disposition mechanism (`supersede`/`retract`), but wiring auto-detection into the weekly digest is an N² scan not justified at this scale. Candidate follow-up.

3. **Where exactly does the fact_store tool handler live at runtime?**
   → **Resolved.** Source `src/plugins/memory/hermes_memory/__init__.py` (renamed from `holographic/`); handler `_handle_fact_store()`. Runtime copy `~/.hermes/hermes-agent/plugins/memory/hermes_memory/`. **Naming gotcha (romar#186):** `memory.provider` must match the *directory* name `hermes_memory` (underscore), not `name()`'s `hermes-memory` (hyphen). `find_provider_dir()` does a literal path lookup, so the hyphen form silently loads nothing and `fact_store` never registers (every fact stays `retrieval_count = 0`). Fixed 2026-08-23 via `hermes config set memory.provider hermes_memory`.

---

## Post-implementation findings

### romar#186 — provider name mismatch disabled the plugin (2026-08-23)

The read path was independently broken: `memory.provider: hermes-memory` (hyphen) loaded nothing because the directory is `hermes_memory` (underscore). Symptom: `fact_store` never registered as a tool, so `retrieval_count = 0` on 100% of facts even after the Phase 2 fix landed. The 2026-08-23 inventory run still shows 155/155 (100%) "never retrieved" for this reason — the next run (2026-08-30) should reflect real retrieval counts now that the provider loads. This is the same class of "write-only ≠ frozen" failure documented in the fact-store-discipline skill: a store can keep *arriving* facts while its *read* path stays dead.

### Gap #6 — fact store durability (opened 2026-08-24)

**Trigger:** Rune accidentally wiped his local `memory_store.db`. Recovered ~1/3 of 370 facts from a local backup + OKF re-ingest; ~2/3 lost. (Notable: at 370 facts Rune's store was ~2.4× the size we'd documented only days earlier — most of what was lost was fresh, so rebuild cost is low.)

**Root cause:** the documented recovery path — "hydrate via JetStream replay" — was never a real backup. The `agent-memory` stream (`agents.memory.shared.>`) only ever carried ~6 shared facts (8 messages total, last published 2026-08-13), and those aged out past the 7-day retention window. Each agent's local facts lived only in its own `memory_store.db`, with no scheduled snapshot.

**Why this plan missed it:** the closure plan scoped *measurement / interface / staleness*. Durability was implicitly assumed to be covered by JetStream retention — an assumption the incident falsified. JetStream is a 7-day message bus, not a durable fact store.

**Proposed fix (not yet implemented — needs scromp's decision):**
- Weekly `memory_store.db` snapshot (e.g. SQLite `VACUUM INTO` to a timestamped file) to NFS or git, scheduled alongside the existing `fact-store-inventory` cron, with retention (keep last N).
- Codify "JetStream is transport, not backup" in the inter-agent memory doc so the false-recovery assumption doesn't recur.

**Status:** Open.

---

## References

- [dshnayder/kube-agents PR #634](https://github.com/gke-labs/kube-agents/pull/634) — Hindsight memory proposal
- [mastersingh24 review](https://github.com/gke-labs/kube-agents/pull/634#pullrequestreview-4908432202) — PR review calling for comparative benchmark
- [romar#43](https://github.com/sackheads/romar/issues/43) — Plugin naming issue
- [romar#186](https://github.com/sackheads/romar/issues/186) — `memory.provider` hyphen/underscore mismatch disables `fact_store`
- Plugin source: `/shared/agents/common/projects/active/hermes-memory/src/plugins/memory/hermes_memory/`
- Plugin runtime copy: `~/.hermes/hermes-agent/plugins/memory/hermes_memory/`
- Repo: `bnaylor/hermes-memory` (forks: `clomp42/hermes-memory`, `rune42808/hermes-memory`)
