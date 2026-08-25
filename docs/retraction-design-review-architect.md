# Review: Retraction Primitive Design Doc (v2)

**Reviewer:** Summoned architect (Claude Sonnet 4.6, 2026-08-14)
**Document:** `docs/retraction-design.md` (v2, post-Rune-review)
**Date:** 2026-08-14
**Note:** Could not write to `docs/` directory (permission denied for uid=999/agent). Placed here instead; please relay or move to `docs/retraction-design-review-architect.md`.

---

## Overall

The core design is sound. "Supersession is a link, not a delete" is the right abstraction — strictly stronger than Hindsight's write-time delete. Rune's v1 review caught the load-bearing gap (`_score_facts_by_vector`) and the lineage cycle problem, both correctly incorporated in v2.

Four issues remain that are worth resolving before implementation. One is correctness-level. Three are clarification gaps that will create implementation ambiguity or audit surprises.

---

## Findings

### 1. [HIGH] `restore` breaks the forward chain without retiring the detached tail

The design specifies that `restore` clears both `superseded_by` and `superseded_at` (§5). This is correct for a leaf node. For a mid-chain node it creates silent breakage.

Concrete case: `A --superseded_by--> B --superseded_by--> C (active)`.

After `restore(B)`:
- B: `status='active'`, `superseded_by=NULL` (cleared per design)
- A: `superseded_by=B` (unchanged)
- C: `status='active'` (unchanged — restore doesn't touch it)

The chain from A now terminates at B (active, no forward pointer). C is still active but detached — no longer reachable from A via the chain, even though it was created specifically to supersede B. Retrieval suppresses A (superseded) and surfaces both B and C as independent active facts, with no signal they were ever related.

The design calls this out in §12 decision 3 ("warn softly, don't block — return a note that this leaves N active versions") but does not address what happens to the broken forward chain. Two credible paths:

**Option A:** `restore` does NOT clear `superseded_by` — it only flips `status`. B becomes active while still pointing forward to C. Chain A→B→C is intact. Current-fact resolution still finds C as the leaf. Two facts are simultaneously active; operator can explicitly `retract(C)` to finish.

**Option B:** `restore` clears `superseded_by` AND retracts everything forward in the chain (C and any downstream). More aggressive but unambiguous.

Option A is simpler and more in keeping with the "warn, don't block" decision already made for non-leaf restores.

**The design must be explicit about C's fate.** Currently underspecified; the implementation will guess.

---

### 2. [MEDIUM] §7.4 collision-at-retrieval must not delegate to `contradict`

§7.4 says the annotation is "cheap: computed on the ≤10-result set, never the corpus." But the only contradiction-detection function available is `contradict`, which scans up to 500 facts in an O(n²) pass (retrieval.py:366). If an implementer reaches for `contradict` to service §7.4, every `search`/`probe`/`related` call pays that cost.

At 120 facts, `contradict` does ~7,000 comparisons; a pairwise check on 10 results does 45. As the corpus grows toward the 500-fact guard rail, that gap becomes ~62,500 vs. 45 on *every* retrieval.

Suggested addition to §7.4: "This is implemented as a pairwise HRR similarity check over the returned result set (`results[i]` vs `results[j]` for i < j, ≤45 pairs at limit=10). It does not call `contradict`. The contradiction score formula is identical but scoped to the result slice."

---

### 3. [MEDIUM] No `CHECK` constraint on the `status` column

The design specifies `status TEXT NOT NULL DEFAULT 'active'` with a three-value enum (§4). SQLite does not enforce TEXT enums natively. Without `CHECK (status IN ('active', 'retracted', 'superseded'))`, a typo in the tool handler silently inserts a fact with an invalid status. That fact vanishes from all recall paths — no error, no warning, just a missing fact.

Fix: add `CHECK (status IN ('active', 'retracted', 'superseded'))` to the `ALTER TABLE` in §4. Idempotent additive migration is unaffected; the constraint is part of the column definition.

This is the cheapest correctness hedge in the whole design. At 120 facts, a silently-suppressed fact is a real operational hazard.

---

### 4. [LOW] `include_superseded` flag is the wrong scope boundary

§6 site #7 specifies `list_facts(include_superseded=True, default off)`. But `retracted` and `superseded` are distinct states. An operator wanting to audit "what was retracted and why" — to verify a deletion decision or recover a mistakenly retracted fact — cannot do so with `include_superseded=True` alone.

Recommend renaming to `include_inactive=True`. Covers both states. One flag, no ambiguity.

---

### 5. [LOW/informational] Bounded traversal hop cap unspecified

§8 references "a visited-set and a hop cap" for cycle-safe current-fact resolution but does not specify the cap value. This will be invented during implementation, making behavior under long-but-valid chains unpredictable without reading the code.

Suggest: "The hop cap is N=50. A legitimate chain will never approach this; exceeding it is treated as a cycle anomaly and logged."

---

## What is solid (don't change)

- §3 "supersession is a link, not a delete" — strictly stronger than Hindsight.
- §5 atomicity note — correct that the supersede UPDATE must land in the same transaction as the INSERT, before the commit at store.py:171.
- §6's rejection of trust-collapse — verified against code: `probe`/`related`/`reason` use trust as a score multiplier, not a filter. `status` column is the only uniform suppression mechanism.
- §7 operational model — "contradiction proposes, a disposition records" is the correct invariant.
- §7.3 digest cadence — growth-gated + weekly ceiling is the right tradeoff.
- Schema migration — idempotent additive `ALTER TABLE` guarded by `PRAGMA table_info`, same pattern as `hrr_vector` (store.py:136).
- FTS5 trigger analysis — triggers copy only content/tags, no trigger change needed. Verified.
- §8 cycle guard — two-defense approach (pre-check on `supersede` + bounded traversal) is correct.

---

## Summary

| # | Severity | Gap | Fix |
|---|---|---|---|
| 1 | HIGH | `restore` breaks forward chain for non-leaf nodes; C's fate unspecified | Choose Option A (don't clear `superseded_by`) or B (retract forward chain); document |
| 2 | MEDIUM | §7.4 may be implemented as full `contradict` scan | Add explicit language: "pairwise check on result slice, not `contradict`" |
| 3 | MEDIUM | No CHECK constraint on `status` | `CHECK (status IN ('active', 'retracted', 'superseded'))` in ALTER TABLE |
| 4 | LOW | `include_superseded` doesn't cover `retracted` | Rename to `include_inactive` |
| 5 | LOW | Hop cap unspecified | Add "N=50" to §8 |

None require redesign. Finding 1 needs a decision (Option A vs B); the rest are clarifications. Recommend resolving 1–3 in the doc before implementation begins.
