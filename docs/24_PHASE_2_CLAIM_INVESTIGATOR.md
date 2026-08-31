# Phase 2 — the hybrid claim investigator

Phase 1 built `RepositoryIndex` and `RepositoryInvestigator` and nothing consumed them.
Phase 2 makes claim discovery use that foundation.

**Nothing downstream of claim selection is touched.** The evidence gate, candidate
generation, the deterministic validator, BASE/HEAD execution, the mechanical classifier,
and the semantic assessor are unchanged and unaware of this work.

---

## Why claim discovery was the bottleneck

Every abstention in the frozen v1 result occurred *before* a candidate was ever
generated. The system was not failing to write tests; it was failing to decide what to
test. Claim selection received `DeterministicContextRetriever`'s diff-shaped snippets,
which answer "what changed" — not the question selection actually has to answer:

> Which observable behavior, reachable through an interface that exists on **both**
> revisions, can distinguish BASE from HEAD?

---

## Architecture

```
immutable BASE + HEAD
        ↓
RepositoryIndex(BASE) / RepositoryIndex(HEAD)          ← Phase 1, unchanged
        ↓
RepositoryInvestigator                                  ← Phase 1, unchanged
        ↓
DeterministicInvestigationPlanner                       ← NEW  claim_investigation.py
   ranked shared observables · HEAD-only reachability
   related tests · partial-index coverage
        ↓
ClaimInvestigator  (bounded loop, PatchProof-owned)     ← NEW  claim_investigator.py
        ├── InvestigationToolbox                        ← NEW  investigation_toolbox.py
        │      inspect_symbol · find_references
        │      find_related_tests · inspect_source_window
        └── AdkGeminiClaimInvestigator                   ← NEW  adk_claim_investigator.py
        ↓
ClaimAgentResult  ── identical type to the v1 agent ──▶ EXISTING PIPELINE (untouched)
```

### Why PatchProof drives the loop instead of ADK's function-calling runtime

ADK can run a native tool loop. It was considered and rejected for three reasons:

1. **Budget enforcement would move provider-side.** The per-tool and total-call limits are
   part of the security contract; they must be enforced where PatchProof can guarantee them.
2. **Replay would depend on provider transcripts** rather than PatchProof's own
   content-addressed record.
3. **Nothing in CI could exercise the orchestration** — every test would need live credentials.

Each model turn returns one structured decision: a small batch of tool calls, or a final
claim/abstention. PatchProof executes the tools and re-invokes. ADK receives `tools=[]`;
the provider is handed data and a schema, never a capability. Swapping to a native loop
later touches only `adk_claim_investigator.py`.

---

## Grounding: identity is `path` + `qualified_name`

The pre-Phase-2 path compared **bare leaf names**, so an unrelated `Config.render` and
`Report.render` were indistinguishable. `ObservableIdentity` is now the only accepted
identity, and the claim schema carries `interface_path` and `interface_qualified_name` as
separate fields rather than one free-text string.

Three rejections are mechanical, before any candidate is generated:

| Rejection | Condition |
|---|---|
| not a shared observable | the identity is absent from the deterministic starting context |
| only on HEAD | the identity appears in `new_head_symbols` |
| not a valid repository symbol | the path is not a normalized repository Python path |

A HEAD-only helper can never be the differential interface — BASE can only fail to
resolve it. But it remains strong *signal*: the planner traces its HEAD callers back to
shared observables and promotes those, which is how "the PR added a helper" becomes a
testable claim about the behavior the helper changed.

---

## Observable preference

Strongest first. The model must justify dropping to a weaker rank.

| Rank | Meaning |
|---|---|
| `EXPORTED_SHARED` | named in the module's `__all__` |
| `PUBLIC_SHARED` | public name, no `__all__` declared |
| `INTERNAL_SHARED` | public name explicitly excluded from a declared `__all__` |
| `PRIVATE_SHARED` | underscore-prefixed |

**Members inherit their parent's visibility.** `__all__` is a module-level notion, so a
method can never appear in it. Ranking every public method of an exported class as
`INTERNAL_SHARED` would mislabel the most useful interfaces in the repository, so a public
member of an exported or public parent is ranked `PUBLIC_SHARED`.

All seven kinds are supported, including **`MODULE_VALUE`**: a module-level singleton
legitimately anchors copy, pickle, identity, equality, and repr behavior. Excluding
non-callables would make that whole class of claim impossible.

### Which observables are offered

An observable qualifies when **any** of:

- its own implementation changed;
- it statically reaches a HEAD-only symbol;
- its definition references some other symbol the pull request changed.

The third rule is what lets a **public caller** be preferred over the changed private
callee it delegates to — the entire point of preferring an externally visible interface.
It is also how a module-level singleton is recognized as affected when the edit landed on
its *class* rather than on its assignment.

---

## Tool security

The toolbox is the complete capability surface. It exposes four read-only lookups against
two prebuilt in-memory indexes. There is no execution boundary for a prompt to cross.

**Not available, and not expressible:** shell, subprocess, arbitrary Python, network,
arbitrary filesystem paths, Git, repository-root selection, BASE/HEAD SHA selection,
environment control, index-budget control.

`revision` accepts exactly `BASE` or `HEAD` (case-insensitive, whitespace-trimmed). A SHA,
a ref, `HEAD~1`, or anything else is refused. Every path must already exist in the index.

### Budgets

| Limit | Default |
|---|---|
| turns | 4 |
| total tool calls | 8 |
| calls per tool | 3 |
| results per call | 10 |
| source window lines | 80 |
| aggregate source characters | 24,000 |

Exhaustion is **not** an error. It returns a structured refusal, and the orchestrator
grants one final turn to conclude from what was gathered — so a budget-limited run ends
conservatively rather than failing.

### Uncertainty semantics

Phase 1's semantics are preserved verbatim. `POSSIBLE_*` match types survive
serialization unchanged. Every result carries `index_partial` and `absence_is_not_proof`
in-band, and the instruction states that absence from a partial index is not evidence of
absence.

---

## Files

**Added**

| File | Purpose |
|---|---|
| `src/patchproof/claim_investigation.py` | ranking, identity, deterministic starting context |
| `src/patchproof/investigation_toolbox.py` | bounded capability surface, budgets, transcript |
| `src/patchproof/claim_investigator.py` | orchestration loop, grounding, normalization |
| `src/patchproof/adk_claim_investigator.py` | production ADK/Gemini turn boundary |
| `tests/test_claim_investigation.py` | 21 tests |
| `tests/test_investigation_toolbox.py` | 59 tests |
| `tests/test_claim_investigator.py` | 22 tests |
| `tests/test_adk_claim_investigator.py` | 14 tests |

**Modified:** none. Phase 2 is purely additive; the v1 claim agent remains available and
its contract is pinned by a test.

---

## What is deliberately not done

Per the scope constraint: no dashboard or UI, no arbitrary public-PR execution, no Cloud
Run or Firestore work, no evaluation-corpus redesign, no negative controls, no embeddings,
no mutation testing, no dynamic code execution, no competing agents, no multi-claim search,
and no cleanup unrelated to Phase 2. No v1 infrastructure was deleted.

`EvidenceWorkflow` is **not** rewired to call the investigator in this branch. The
investigator emits the identical `ClaimAgentResult` type, so the swap is a one-line
injection — but making it the default is a behavioral change to the production path that
belongs with the live smoke run, not ahead of it.

---

## Known limitations

- **No live Gemini validation.** No credentials in this environment; the orchestration is
  proven offline against a scripted model at the same boundary the ADK adapter implements.
- **`find_callers` and `compare_symbol` are not exposed to the model.** The planner uses
  `find_callers` deterministically; widening the model surface was not justified yet.
- **Reachability is one hop and syntactic.** A behavior reached through two levels of
  indirection will not be linked, and dynamic dispatch is invisible — by design, and
  labelled as such.
- **The planner is not wired into the production workflow** (see above).
- **Related-test discovery reads HEAD**, which in production includes the pull request's
  own new tests. That makes production easier than a holdout and should be stated in any
  evaluation write-up.
