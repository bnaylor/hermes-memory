# Review: Retraction Primitive Design Doc

**Reviewer:** Rune
**Date:** 2026-08-13
**Document under review:** `docs/retraction-design.md` (Clomp)

---

## Overall

Sound design, correct core insight. "Supersession is a link, not a delete" is strictly stronger than Hindsight's write-time delete — same contamination suppression, plus a reversible, followable audit trail. §7 answers scromp's actual question ("who supersedes, on what signal") with the right answer: the writer is the operator, scromp's involvement is optional at every layer.

One load-bearing correctness gap in §6, two lineage edge cases, and four open questions — all resolvable without redesign. **Approve with revisions.**

---

## Findings

**1. [HIGH] §6 misses `_score_facts_by_vector` (retrieval.py:474).**
The doc lists four filter sites — `_fts_candidates`, `probe` (156), `related` (218), `reason` (299) — but there are **five** HRR read paths. `_score_facts_by_vector` (line 474) has its own `WHERE hrr_vector IS NOT NULL` scan and is the *actual path `probe` takes when called with a `category`* (probe delegates to it at line 151). Implemented literally, `probe(entity, category=X)` leaks superseded facts — violating goal #1.

Fix: add `_score_facts_by_vector` to the §6 list (or restate as "all five read paths").

**2. [MEDIUM] Lineage has no cycle guard.**
§8 resolves "current fact" by following `superseded_by` to a leaf — but nothing prevents `supersede(A, by=B)` then `supersede(B, by=A)`, which creates a cycle with no leaf (infinite traversal during current-fact resolution). Add a rejection on `supersede` when `by_fact_id` transitively points back to `fact_id`, or bound the traversal depth and document it.

**3. [MEDIUM] `add(supersedes=…)` atomicity needs deliberate implementation.**
The doc claims a "single atomic transaction" with "no window," but current `add_fact` commits immediately after INSERT (store.py:171), *before* entity/HRR/bank work. To honor the claim, the supersede UPDATE must land in the same transaction as the INSERT, before that first commit. Restructure `add_fact` accordingly.

**4. [LOW] `contradict`'s digest will re-propose already-disposed pairs.**
`contradict` scans `WHERE f.hrr_vector IS NOT NULL` (line 372) with no status filter, so a superseded fact and its superseder remain a "contradiction" forever. Scope the §7.3 digest to active facts (`AND f.status = 'active'`), or the digest re-surfaces dispositions already recorded.

**5. [LOW/informational] `store.search_facts` (store.py:215) is not on the live handler path.**
Confirmed this session: no `.py` caller outside its definition — the `search` action routes through `FactRetriever.search()` → `_fts_candidates`. Filtering it is correct-but-defensive; the load-bearing filter is `_fts_candidates`. Don't let the implementer mistake `search_facts` for the live path and under-test the real one.

---

## Open questions — my answers

1. **`add(supersedes=…)` — include it.** Atomicity is worth it and it matches §7.1's write-time model. (See finding 3 for the implementation caveat.)
2. **`update` vs `supersede` — the split is right; keep `update` lineage-free.** "Fixed a typo in this record" and "the answer changed, here's the replacement" are different audit events. Making `update` emit lineage would pollute the trail. Material content changes route through `supersede`; §5 already implies this.
3. **`restore` of non-leaf — warn softly, don't block.** Return a note ("this leaves N active versions") in the response. Zero review burden, prevents silent two-active-versions confusion.
4. **Digest cadence — growth-gated with a weekly ceiling.** "Only when new facts arrived" alone risks never firing if facts trickle; weekly alone is noise. Run when fact-count grew since last digest, at most weekly regardless.

---

## What's correct (don't change)

- §3 "supersession is a link, not a delete" — the right posture, strictly stronger than Hindsight.
- §7 — the four-trigger ladder and "contradiction proposes, a disposition records" is exactly the operational answer scromp asked for.
- §6's trust-collapse rejection — verified against code: probe/related/reason use trust as a score *multiplier*, not a filter, so a status column is the only uniform suppression mechanism.
- `status` TEXT enum over a boolean flag — correctly separates "retracted" (no replacement) from "superseded" (has lineage).
- FTS5-trigger claim verified — triggers copy only content/tags, so no trigger change needed.
- Migration is idempotent + additive, mirroring the existing hrr_vector pattern (store.py:136).

---

## Verified this session (evidence)

- fact count **120** (doc's "120 facts" — accurate); columns confirmed pre-migration.
- `WHERE hrr_vector IS NOT NULL` present at exactly 4 sites: **156, 218, 299, 474** (the 4th is the missing one).
- `search_facts` has no live `.py` caller (only a `.pyc` binary match).
- `remove_fact` hard-deletes, `update_fact` rewrites in place, `contradict` mutates nothing — all as the doc states.
