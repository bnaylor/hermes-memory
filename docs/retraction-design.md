# Retraction Primitive — Design Doc

**Status:** Draft v3 — Rune + architect reviews incorporated, ready for consensus
**Author:** Clomp
**Reviewers:** Rune (reviewed), architect "Claudy" (reviewed), scromp
**Date:** 2026-08-13
**Repos:** `bnaylor/hermes-memory` (source: `src/plugins/memory/hermes_memory/`)

---

## 1. Problem

We have no way to say "this fact is no longer true" except:

- `remove(fact_id)` — a hard delete. Loses the record that the fact was ever held, and requires the caller to already know the `fact_id`.
- `update(fact_id, content=…)` — in-place rewrite. Silently overwrites; no record that a *prior* claim existed.

`contradict` is detection-only: it finds pairs that share entities but diverge in content, returns them for review, and mutates nothing.

The kube-agents bake-off made the cost concrete. Hindsight wins supersession probes by *deleting* retired content at write time. We keep every version, and FTS5 returns them all — so a query like "what's the service-account-key policy" returns three versions, and nothing tells the model which is current. For a fleet agent the wrong answer is a *previously correct* one. That is the governing failure mode we are closing here.

## 2. Goals / non-goals

**Goals**

1. Superseded or retracted facts never surface on any recall path.
2. The relationship is *reversible* and *auditable* — full lineage from superseded → current.
3. Zero standing human-review burden. (scromp: "I do not want to review all the facts all the time.")

**Non-goals**

- Automatic consolidation / merge-on-write (Hindsight's identifier-collapse failure — 162→50 IDs). Explicitly rejected; Thread A already recorded "we're not touching it."
- Embedding-similarity auto-detection of supersession.
- Multi-tenant / per-user authority ACLs.

## 3. Design principle

**Supersession is a link, not a delete.**

Hindsight deletes retired content at write time — it loses history and has no "un-retire." We store the fact, mark its lifecycle state, and record *what replaced it*. This is strictly stronger: same recall-suppression guarantee, plus reversible audit trail and a followable lineage to the current version.

## 4. Schema changes

Additive `ALTER TABLE` only — no data migration, existing rows are untouched.

```sql
ALTER TABLE facts ADD COLUMN status        TEXT      NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted', 'superseded'));
ALTER TABLE facts ADD COLUMN superseded_by INTEGER   REFERENCES facts(fact_id);
ALTER TABLE facts ADD COLUMN superseded_at TIMESTAMP;
```

- `status` ∈ `'active'` | `'retracted'` | `'superseded'`.
- `superseded_by` → the `fact_id` of the replacing fact (NULL for `retracted`).
- `superseded_at` → timestamp of the disposition, for audit.
- The `CHECK` constraint makes a typo in the tool handler fail loudly at write time, instead of silently inserting an invalid status that vanishes from every recall path (SQLite does not enforce TEXT enums natively).

FTS5 is unaffected: the `facts_fts` triggers already copy only `content`/`tags`, so no trigger change. Suppression happens in the `SELECT` `WHERE`, not in the index — which is what keeps it reversible (flip `status` back, the fact re-enters recall immediately).

Existing 120 facts default to `'active'` — zero data loss, zero behavioral change on rollout.

## 5. Tool surface

New `fact_store` actions (schema enum extended):

| Action | Args | Effect |
|---|---|---|
| `retract` | `fact_id` | `status='retracted'`, `superseded_by=NULL`, `superseded_at=now` |
| `supersede` | `fact_id`, `by_fact_id` | `status='superseded'`, `superseded_by=by_fact_id`, `superseded_at=now` |
| `restore` | `fact_id` | `status='active'`, clear `superseded_by`, `superseded_at` (see §8 for mid-chain semantics) |

`supersede` rejects self-supersession (`fact_id == by_fact_id`) and any assignment that would create a lineage cycle (see §8).

Plus an optional convenience parameter on `add`:

- `add(content=…, supersedes=old_fact_id)` — inserts the new fact *and* supersedes the old one in a single round-trip, with no window where the new fact is live but the old one isn't yet retired.

**Atomicity note (from review):** `add_fact` currently commits immediately after the INSERT (store.py:171), *before* entity/HRR work. To make `add(supersedes=…)` genuinely atomic, the supersede UPDATE must land in the **same transaction as the INSERT, before that first commit** — i.e. `add_fact` gains a `supersedes` parameter and issues the INSERT + supersede UPDATE together, then commits once. Entity/HRR/vector work stays after the commit. Invariant: either the new fact is `active` *and* the old one `superseded`, or neither.

Existing `update` keeps its meaning: **in-place correction of the *same* fact** (the fact_id is stable — "this record was wrong, I'm fixing its value"). `supersede` is the *different* path: "the answer changed, here is the before and the after." The distinction is the audit story, not a technical constraint.

## 6. Retrieval semantics (the load-bearing line)

Every recall path filters to active facts. Line numbers are against current `main` (2026-08-13); they are guidance, not contract.

| # | Read path | Site | Filter |
|---|---|---|---|
| 1 | `search` → `_fts_candidates` | retrieval.py:521 (`WHERE` clauses) | add `f.status = 'active'` |
| 2 | `probe` (no category) | retrieval.py:156/162 | add `status = 'active'` |
| 3 | `probe` (with category) → `_score_facts_by_vector` | retrieval.py:474 | add `status = 'active'` |
| 4 | `related` | retrieval.py:218 | add `status = 'active'` |
| 5 | `reason` | retrieval.py:299 | add `status = 'active'` |
| 6 | `store.search_facts` | store.py:215 | add `status = 'active'` |
| 7 | `store.list_facts` | store.py:341 | default `status = 'active'`; `include_inactive` flag (default off) |

Site #3 was Rune's catch — `_score_facts_by_vector` carries its own `WHERE hrr_vector IS NOT NULL` scan and is the path `probe` actually takes when a `category` is supplied. Omit it and `probe(entity, category=X)` leaks superseded facts, violating goal #1.

Two further notes:

- `contradict` (retrieval.py:366/372) scopes its scan to `status = 'active'`, so the §7.3 digest converges instead of re-proposing pairs that were already disposed. (Low severity, but it is the difference between a digest that stops nagging and one that doesn't.)
- `include_inactive` (not `include_superseded`) covers both `retracted` and `superseded` in one flag. Rows carry `status`, so the operator can distinguish the two states in the result.
- This is why a *trust-collapse* hack was rejected: `probe`/`related`/`reason` do not apply `min_trust` — they use trust only as a score multiplier — so zeroing trust would still leak superseded facts through the entity paths. A `status` column is the only mechanism that suppresses uniformly across all seven read sites.

## 7. Operational model — who supersedes, on what signal

This is the crux. The primitive has **one operator — the writer, at the moment of write** — and three triggers, ordered by how much standing attention each costs (zero → bounded → near-zero). None of them require scromp to review facts continuously.

### 7.1 Write-time correction — *zero* human attention

When an agent stores a fact that supersedes one it already holds, it declares the link in the same call. The signal is the writer's own knowledge: it just learned a new value, or it is correcting a fact it wrote earlier.

This is no more privileged than `update`/`remove` are today — the agent already has write authority and already self-serves without a human in the loop. The only change is it now has a primitive to *record lineage* instead of silently overwriting or hard-deleting.

Example: agent stored "deploy uses helm v3"; user says "we moved to helm v4"; agent calls `add("deploy uses helm v4", supersedes=old_id)`.

### 7.2 Prose-declared supersession — *zero* human attention

When incoming content *states* the supersession ("v2 supersedes v1", "ADR-2026-052 obsoletes ADR-2024-014"), the writer resolves the reference with `probe`/`search`, then links it. The signal is the content itself. This is exactly the case the bake-off corpus is built on, and it means the harness can be run honestly — the supersession links are declared, not auto-inferred.

### 7.3 Contradiction safety net — *bounded*, delegable attention

`contradict` stays detection-only and becomes a **scheduled, threshold-gated** pass (cron), not a per-turn behavior. It emits a short ranked digest of candidate pairs above a materiality threshold (entity overlap + content divergence), scoped to `status = 'active'` so already-disposed pairs drop out.

The reviewer is an **agent (the curator)**, not scromp. The curator *proposes* supersessions; scromp sees a digest and approves/rejects a batch — or delegates the whole disposition. The binding rule: **contradiction proposes, a disposition records.** Nothing auto-applies on `contradict` alone. There is no path from "similar facts" to "retired fact" that does not pass through an explicit `supersede`/`retract` call.

This is the "identify and close the gap" loop made operational: the digest surfaces *candidate* gaps, the disposition closes them.

### 7.4 Collision-at-retrieval — *near-zero*, fires only on real conflict

When a recall path returns two facts that would be flagged contradictory, annotate the pair in the tool response. **Implementation:** a pairwise entity-overlap + content-divergence check over the ≤10-result slice (≤45 pairs at `limit=10`), computed *inside* the retrieval method **before** `hrr_vector` is stripped — same formula as `contradict`, scoped to the result set. It must **not** call `contradict`, which is an O(n²) corpus scan of up to 500 facts; at 120 facts that is ~7,000 comparisons vs 45, and the gap widens to ~62,500 vs 45 near the guard rail. When numpy/HRR is unavailable, skip the annotation. The agent, at use time, issues `supersede`/`retract` if it judges one current. Zero standing review — it only triggers when two facts actually collide in a result set.

### 7.5 Ruled out explicitly

"Review all facts all the time" is not required by any layer. 7.1–7.2 are self-serve (the agent is the operator). 7.3 is a periodic digest of *proposals*, delegable to an agent. 7.4 fires only on collision. scromp's involvement is optional at every layer except, at most, a batch-approve he can also hand off.

## 8. Lineage and current-fact resolution

`superseded_by` forms a linked list. The **current** fact is the last `active` node reached by following `superseded_by` to its leaf.

```
A --superseded_by--> B --superseded_by--> C(active)
```
`A` and `B` are suppressed; `C` is the answer. If `C` is later superseded by `D`, `B`'s pointer is untouched — the chain simply extends.

**Cycle guard (from review):** `supersede(A, by=B)` then `supersede(B, by=A)` would create a loop with no leaf, so "follow to leaf" would never terminate. Two defenses:

1. `supersede` rejects any assignment where following `superseded_by` forward from `by_fact_id` reaches `fact_id` (would close a cycle). Self-supersession (`fact_id == by_fact_id`) is rejected outright.
2. "Current fact" resolution uses a **bounded traversal** — a visited-set and a hop cap of **N=50** — and *fails closed*: on detecting a cycle it returns the most-recently-visited active node and flags the anomaly rather than looping. A legitimate chain will never approach 50; exceeding it is treated as a cycle anomaly and logged.

**Restore and the forward chain (resolved in architect review):** `restore` flips `status='active'` and clears `superseded_by`/`superseded_at`. For a leaf this is clean. For a mid-chain node it severs the forward link: `A→B→C(active)` + `restore(B)` yields `A→B(active)` with `C` still independently `active`. This is the *correct* consequence, not a defect — the operator declared B current again, so "B was superseded by C" is now false. The `restore` response therefore names the orphaned tail explicitly ("restored B; C (id=N) was superseding it and remains active — retract C if stale") rather than leaving the operator to discover the orphan.

Rejected alternatives: (a) auto-retracting the forward chain — over-aggressive, contradicts the §12 "warn, don't block" decision; (b) keeping `superseded_by` after restore — leaves an active fact carrying a non-null `superseded_by`, breaking the invariant `superseded_by` set ⟺ `status='superseded'`.

## 9. Migration & backward compatibility

- Idempotent `ALTER TABLE … ADD COLUMN` guarded by a `PRAGMA table_info` check (same pattern as the existing `hrr_vector` migration, store.py:136).
- No FTS rebuild, no trigger change, no HRR/vector recompute.
- Existing behavior for `add`/`search`/`probe`/`related`/`reason`/`update`/`remove`/`list` is unchanged for active facts.
- Rollout is safe to deploy before any caller adopts the new actions.

## 10. Testing plan

1. **Unit — state machine.** `retract`/`supersede`/`restore` transitions; `superseded_at` set/cleared; `supersede` rejects unknown `by_fact_id`, self-supersession, and cycles; `add(supersedes=…)` is atomic (both effects or neither); invalid `status` value raises (CHECK constraint).
2. **Unit — suppression.** Seed a three-version supersession chain; assert `search`, `probe` (both with and without `category` — exercises site #3), `related`, `reason`, and `list` return only the active version; `list(include_inactive=true)` returns all three with correct `superseded_by` pointers and `status`.
3. **Unit — reversibility.** `restore` re-surfaces a fact on all paths; no FTS corruption (the retired fact's text still matches, but is filtered). `restore` of a mid-chain node leaves the tail active and names it in the response.
4. **Unit — migration idempotency.** Re-run init against an existing DB; columns present, no duplicates, all rows `active`.
5. **Integration — the harness.** After this lands, build the seed adapter to *declare* supersession links (`add(supersedes=…)`), then run Dmitry's 26 probes. Expect the supersession class to pass cleanly — and expect the *synthesis* class to be where we genuinely contend, since that exercises `reason`/entity-resolution, which Hindsight lacks.

## 11. Out of scope / deferred

- **Auto-detection / consolidation** — gated behind a future decision; not built here.
- **Embedding-similarity suggestion** of supersession candidates.
- **Authority/ACL gating** on who may retract (single-human homelab; the writer and scromp share one authority domain).
- **Provenance of *who* superseded** (agent/session id) — trivially addable via one more column if we later want it; not blocking.

## 12. Decisions (resolved in review)

1. **`add(supersedes=…)` vs. two calls** → **Include it.** Atomicity is worth the single-parameter surface; implement in one transaction per the §5 note.
2. **`update` vs `supersede` boundary** → **Keep split; `update` stays lineage-free.** "Fixed a typo" ≠ "the answer changed." `update` mutates the same fact in place; `supersede` records that a *different* fact replaced it.
3. **`restore` of a non-leaf node** → **Warn softly, don't block.** Return a note naming the orphaned tail; do not refuse.
4. **§7.3 digest cadence** → **Growth-gated with a weekly ceiling.** Emit only when a batch of new facts has arrived since the last digest, but never more often than weekly — pure growth-gating can starve, pure weekly is noise.
5. **`restore` forward-chain semantics** → **Clear `superseded_by`, keep the tail active, name it in the response.** Do not auto-retract the tail, do not retain a stale `superseded_by` on an active fact (see §8).
