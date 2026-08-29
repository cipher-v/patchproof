# Generalization hardening — implementation plan and change log

This document records the work carried out on the `feat/generalization-hardening`
branch in response to `docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md`.

It is a change log, not a results claim. **No number in this document asserts that
PatchProof generalizes better.** The only artifact that could establish that is a
fresh, sealed, environment-ready holdout run under the protocol in §6 below, which
has not been run at the time of writing.

## Non-negotiable invariants preserved by every change here

These were verified before and after each stage:

1. A positive outcome still requires `BASE = ASSERTION_FAILED`, `HEAD = PASSED`,
   `mechanical_status = DISCRIMINATING`, and `assertion_relation = RELATED`.
2. `BASE TEST_ERROR + HEAD PASSED` is still **never** support. No stage relaxes this.
3. The model still cannot propose, influence, or observe an install or test command.
4. Candidate source still cannot modify production source or existing tests.
5. Candidate attempts remain bounded at one initial plus two repairs.
6. `benchmarks/` is untouched. Every sealed manifest, journal, oracle hash, raw
   result, and V1-V5 artifact is preserved byte-for-byte.
7. No repository-specific, case-specific, or claim-text-specific branching was added
   anywhere in `src/`.

## Stage 0 — Explicit per-task reasoning budgets

**Problem.** All three ADK adapters hard-coded `ThinkingLevel.LOW` and a local
output cap. The sealed holdout recorded 6,205 output tokens across 30 provider calls
(~207 output tokens per call). At that budget it is not possible to attribute the
observed failures to model capability rather than to configuration, so every
downstream conclusion about "Gemini could not do X" was unsafe.

**Change.** Added `src/patchproof/reasoning_budget.py`: a single, documented,
testable place where each of the three semantic tasks declares its thinking level,
output cap, and temperature. The three ADK adapters now derive their defaults from
it and accept a `reasoning_budget` override.

| Task | Thinking level | Output cap | Rationale |
|---|---|---|---|
| `CLAIM_SELECTION` | LOW → **MEDIUM** | 2,048 → **4,096** | Hardest reasoning step; must commit to a falsifiable differential hypothesis, and the claim schema grew (Stage 4) |
| `CANDIDATE_GENERATION` | LOW → **MEDIUM** | 12,000 (unchanged) | Initial candidate is near-mechanical, but repair must diagnose bounded execution evidence |
| `EVIDENCE_ASSESSMENT` | **LOW** (unchanged) | 4,096 (unchanged) | Answers one narrow relatedness question against already-established facts. Extra deliberation cannot widen what the mechanical layer permits, so a more talkative assessor is a false-positive risk, not a benefit |

**Why this generalizes.** It is a capability change with no repository-specific
content. It applies identically to every PR.

**False-positive risk.** Low. The mechanical gate and
`_validate_semantic_decision` are untouched. The assessor was deliberately left at
LOW for exactly this reason.

**Why it is staged first.** It is the control condition. Running the ablation with
Stage 0 alone, before any other change, is what makes it possible to attribute later
gains honestly rather than crediting them to the wrong stage.

**Tests.** `tests/test_reasoning_budget.py` — every task declares exactly one
budget; reasoning-intensive tasks are not left at LOW; assessment stays at LOW; the
adapters actually apply the declared settings; out-of-range budgets are rejected.
