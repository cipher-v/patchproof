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

`EvidenceWorkflow` **is** now wired to the investigator (see "Production wiring" below).

---

## Known limitations

- **No live Gemini validation.** No credentials in this environment; the orchestration is
  proven offline against a scripted model at the same boundary the ADK adapter implements.
- **`find_callers` and `compare_symbol` are not exposed to the model.** The planner uses
  `find_callers` deterministically; widening the model surface was not justified yet.
- **Reachability is one hop and syntactic.** A behavior reached through two levels of
  indirection will not be linked, and dynamic dispatch is invisible — by design, and
  labelled as such.
- **Related-test discovery reads HEAD**, which in production includes the pull request's
  own new tests. That makes production easier than a holdout and should be stated in any
  evaluation write-up.

---

## Production wiring

`EvidenceWorkflow.__init__` gains one optional keyword, `claim_investigator:
ClaimInvestigatorFactory | None = None`. When supplied, claim selection routes through the
investigator; when omitted, the v1 claim agent runs exactly as before. The investigator is
therefore opt-in and the v1 path is preserved, not replaced.

The change inside `execute()` is a single branch around the existing call:

```python
if self.claim_investigator is not None:
    investigation = await self.claim_investigator.build(
        base_sha=run.base_sha, head_sha=run.head_sha
    ).investigate(narrative=narrative, diff=context.diff)
    claim_result = investigation.agent_result
    investigation_transcript = investigation.transcript
else:
    claim_result = await self.claim_agent.select_claim(context=context, narrative=narrative)
```

`claim_result` is a `ClaimAgentResult` either way, so **everything after this line is
byte-for-byte the code that already existed**: candidate generation, the deterministic
validator, BASE/HEAD execution, artifact hashing, the mechanical classifier, the semantic
assessor, and support admissibility.

`GitClaimInvestigatorFactory` is the production implementation. It indexes both revisions
from immutable Git objects using the same `source_repository` and the same
`excluded_paths` the context retriever already uses, so holdout exclusions apply
identically to claim investigation.

## Tool-discovered interface admission

The deterministic starting context is ranked and truncated, so a valid shared observable
can fall outside it. Investigation may now recover one — but only through mechanical proof.

An interface is accepted if **either**:

**A.** it is already in `starting_context.shared_observables`; or

**B.** all four of the following hold:

1. the exact `path::qualified_name` was returned by an actual tool call in this run (the
   model's assertion in its reasoning text has no effect — only PatchProof's own execution
   of a tool populates the discovered set);
2. `RepositoryInvestigator.compare_symbol` re-derives it from the immutable BASE/HEAD
   indexes as `PRESENT_ON_BOTH`;
3. its HEAD kind is an admissible observable kind;
4. it is **relevant to this pull request** — its implementation changed, or its definition
   references a symbol the pull request changed.

Condition 4 exists because the planner omits observables for two different reasons: budget
truncation, and deliberate irrelevance. Discovery must recover the first and never the
second, or a tool result could be used to anchor a claim on any unchanged symbol anywhere
in the repository — including one that merely shares a leaf name with something the pull
request touched.

**HEAD-only rejection is strengthened, not weakened.** On this path it is *proven* from the
indexes (`NEW_ON_HEAD`) rather than inferred from a truncated starting context. Symbols
removed from HEAD are rejected the same way.

## Transcript persistence

`EvidenceReport` gains `investigation_transcript: InvestigationTranscript | None`, excluded
from serialization when absent. It carries exactly four fields:

| Field | Content |
|---|---|
| `turns` | model turn count |
| `tool_calls` | per call: sequence, turn, tool, **validated** arguments, status, match count, truncation, source chars, bounded detail |
| `starting_context_sha256` | hash of the deterministic pre-fetch |
| `response_sha256` | hashes of the raw model responses |

**No chain-of-thought and no free model text.** A test asserts the persisted document
contains neither the model's `reasoning` strings nor its raw responses, and pins the field
set so it cannot silently grow.

Arguments are the sanitized values from `ToolCallRecord`, retaining only short
JSON-serializable scalars. Backward compatibility: the field is optional with a `None`
default, so v1 stored evidence that predates claim investigation loads unchanged — pinned
by a test that strips the field and re-validates.

## Evidence admissibility: unchanged

`evidence.py`, `challenge.py`, `pytest_runner.py`, `test_generation.py`,
`execution_contract.py`, and `benchmarks/` have **zero** changes in this follow-up. The
only deletion anywhere in `evidence_workflow.py` is the claim-selection call that the new
branch replaces. The 39 evidence-gate and classifier invariant tests pass unchanged.
