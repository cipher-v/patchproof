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

## Stage 1 — Reach: make the executor work on repositories PatchProof did not author

Three independent defects, each with a regression test that fails without its fix.

### 1a. A repository's own pytest configuration could abort the run

**Problem.** `ChildProcessEnvironmentPolicy` set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` for
every child process, while the fixed pytest argv never neutralized the repository's own
`addopts`. Any repository declaring a plugin option in `addopts` therefore failed
*argument parsing* and exited before writing a JUnit report, which
`PytestJUnitParser._without_report` reports as `PROCESS_ERROR`. This is the anyio and
cattrs signature in the sealed holdout. Installing dependencies does not help, because
autoload suppression prevents the installed plugins from registering at all.

**Change.** `pytest_runner.DISABLED_PYTEST_PLUGINS` names only the plugins that actually
break comparability (`cacheprovider`, `randomly`, `xdist`, `cov`); autoload stays enabled
so a repository's `conftest.py` can import; `PYTEST_ISOLATION_ARGUMENTS` passes
`-o addopts=`.

**Not a security relaxation.** Repository code, `conftest.py` included, already executes
inside the sandbox by design — that is what a BASE/HEAD challenge *is*. Isolation comes
from the credential-free environment, the private executor identity, and the disposable
worktree, never from plugin suppression.

**Verification.** `tests/test_pytest_isolation.py` builds a repository whose `addopts`
demand an absent plugin option. With the fix reverted the challenge reports
`BASE=PROCESS_ERROR, HEAD=PROCESS_ERROR` — reproducing the holdout failure exactly. With
it, the same pair discriminates normally.

### 1b. Only uv-lockfile repositories could install at all

**Problem.** `_ALLOWED_INSTALL_COMMANDS` admitted only `uv sync --frozen`, requiring a
committed `uv.lock` at *both* revisions. No holdout repository satisfies this, so
dependency installation had only ever run against PatchProof's own repository.

**Change.** `src/patchproof/install_strategy.py` probes committed packaging metadata
(`uv.lock`, `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`) and selects
one of four strategies.

**ADR-004 is preserved exactly.** No model chooses, proposes, influences, or observes an
install command. Commands are assembled from fixed templates; the only variable positions
are a committed requirements path and a repository-declared extra or PEP 735 group name;
every assembled token is validated against an allowlist before execution. Extras are never
invented — a project declaring none is installed without one. A synthesized contract
validates through the same `ExecutionContract` model with `synthesized=True`; a
hand-written `.patchproof.yaml` keeps the narrow literal allowlist and cannot use probed
commands.

**BASE/HEAD symmetry preserved.** Both revisions are probed independently and must agree.
A pull request that changes how the project installs is non-comparable, and PatchProof
abstains rather than comparing two different environments.

### 1c. The readiness gate never proved a candidate could run

**Problem.** `prepare_environment` ran the install commands and stopped. The sealed oracle
gate had the same blind spot in a different form: it executed oracles by absolute path
from *outside* the checkout while candidates were injected *into* it, so the two paths
used different pytest rootdirs and the gate certified ten cases as runnable when eight
could not execute a candidate.

**Change.** The gate now ends by injecting a trivial always-passing probe test through the
*identical* injection point, argument vector, and environment policy a real candidate
uses, and requires `PASSED` on both revisions.

### 1d. Up to eight cold installs per pull request

**Problem.** A fresh worktree pair per operation meant re-installing every time — two for
readiness, two per candidate attempt — all cold (`UV_NO_CACHE=1`), all sharing the single
contract timeout that also bounds the test. On any substantial repository the *install*,
not the test, was what timed out.

**Change.** `ChallengeSession` opens one worktree pair, reused for readiness and every
attempt. `PytestRunner` memoizes completed setups. Each artifact is removed after
execution so reuse cannot let one attempt observe another's file.
`install_timeout_seconds` is a separate contract field (default 900s).

### 1e. Import grounding ignored what was installed

**Problem.** Allowed import roots came from the context bundle alone — first path
components and snippet imports — which has nothing to do with what is importable. The
cattrs initial candidate was rejected with `UNGROUNDED_IMPORT` because `attrs` was absent
from the context, despite being a hard runtime dependency. It never ran.

**Change.** `src/patchproof/environment_introspection.py` reads top-level importable names
from the prepared workspace's `site-packages` layout and `dist-info/top_level.txt`. No
subprocess is spawned and no dependency code is imported, so a hostile package cannot
influence grounding by executing at introspection time. The session **intersects** BASE and
HEAD roots, so a dependency introduced by the pull request cannot make a test that is
structurally unrunnable on BASE look discriminating. Being installed is not permission: the
blocked-import list still rejects network, subprocess, and dynamic-code roots.

## Stage 2 — Give repairs the observed values, and name what actually happened

**Problem.** `_execution_exception` returned `None` unless the status was `TEST_ERROR`. A
repair following `BOTH_PASSED` or `BOTH_ASSERTION_FAILED` — the two situations where
repair is the entire point — received only status names, its own failing line, and the
mechanical status. The jsonschema repair was asked to find a discriminating input while
being told nothing except that its input was not discriminating. **Eight repairs producing
zero discriminations is the predicted consequence of that design, not evidence about the
model.**

**Change.** Feedback now carries, for both revisions: the pytest assertion diff (the
operands of the failed comparison); the deepest traceback frame that is *not* the generated
test — which answers the question a `BOTH_PASSED` repair most needs and could not otherwise
ask, *did my trigger reach the changed code at all*; and pattern-specific guidance
distinguishing a wrong trigger from a wrong expectation.

**Value-preserving sanitization.** The general sanitizer rewrites anything path-shaped and
any long opaque token — right for a traceback frame, wrong for an assertion diff. For
platformdirs the observed value *is* a path; generic redaction would erase exactly the
signal. `_sanitize_observed_value` keeps credential, bearer-token, and provider-token
redaction and control-character stripping, and drops the path/opaque rules. Both halves are
pinned by tests. The observation budget rose from 4x500 to 5x900 characters.

### `TEST_ERROR` on one side is no longer called `ENVIRONMENTAL`

An `IndexError` escaping from the code under test is not a broken environment; it is the
bug, observed on BASE, by the agent's own test. **The mechanical refusal to score it as
support is correct and unchanged.** The label was not: it counted Starlette #3317 and Rich
#3938 as infrastructure noise, and told the repair its environment was broken when the
truth was that the experiment had worked and produced an inadmissible *observation shape*.

`MechanicalEvidenceStatus.UNCAUGHT_EXCEPTION_ON_ONE_REVISION` reports it honestly. Its
pattern remains `NOT_COMPARABLE`, so it can never reach the semantic assessor and can never
become support. `TEST_ERROR` on both revisions remains `ENVIRONMENTAL`.

### Exception-to-observable repair, with mechanical guardrails

The repair for that status teaches a general transformation: bind both outcomes to one
observed value and assert on it. This is what the independent oracle did for Starlette and
what the agent's own repair failed to reach. It is not a rule about any exception,
repository, or claim.

The instruction is safe **only** because its degenerate forms are now impossible in
`CandidateTestValidator`, not merely discouraged in the prompt:

| Rejection | Prevents |
|---|---|
| `BROAD_EXCEPTION_HANDLER` | bare `except`, `except Exception`/`BaseException`, `pytest.raises` with either |
| `UNGROUNDED_EXCEPTION_TYPE` | catching a type that is neither a builtin exception nor imported by the candidate |
| `SWALLOWED_OUTCOME` | a handler that discards the outcome instead of recording it for an assertion |

A test asserts the prompt text and the validator cannot drift apart.

## Stage 3 — Ground the experiment on an interface both revisions have

**Problem.** Signature grounding and every source snippet were derived from HEAD alone;
`_changed_python_context` reads BASE only for deleted files. The model's entire view of
BASE was three lines of unified diff context.

### 3a. Cross-revision interface partition

A pull request that adds a helper presented that helper to the agent with **no mechanism to
ask whether BASE has it**. Rich #3938 tested `Segment.split_lines_terminator`, which BASE
does not define, so the test could only demonstrate that a new method exists — trivially
true on HEAD, impossible on BASE. *Given the architecture, that outcome was guaranteed, not
a lapse of judgment.*

`DeterministicContextRetriever` now parses changed files at both revisions and publishes
`CrossRevisionInterfaces`: `present_on_both`, `new_on_head`, `removed_on_base`. Both agent
prompts are told to express the claim through `present_on_both`, and to ask what observable
behavior a new helper was introduced to change rather than claiming that it exists.

`CandidateTestValidator` enforces it as `HEAD_ONLY_INTERFACE`. The check reads the
deterministic partition and knows nothing about any project, so it generalizes to every
"pull request adds a helper" change — the most common shape there is. **It can only reduce
support, never manufacture it**, and the rejection reaches the repair as actionable feedback
naming the offending symbol.

### 3b. One-hop called-helper bodies

`_reference_snippets` emits a single matched line plus two lines of padding per file and
then breaks — enough to prove a name exists, far too little to convey what it does.
jsonschema #1208 turned on nested container equality implemented in an unchanged helper
whose body the agent never saw. **That is context insufficiency, not weak trigger
synthesis.**

`SnippetKind.CALLED_HELPER` retrieves the full bodies of unchanged repository helpers called
from inside a changed symbol's span — bounded to 3 helpers of at most 700 characters, ranked
above generic name-match references. This is a call-graph lookup, not repository-wide
retrieval: no embedding search, no unbounded traversal. Excluded paths are still removed
before any derivation, and a test pins that the new snippet kind honours holdout exclusions.

## Stage 4 — Make the claim a falsifiable differential hypothesis

**Problem.** Every claim field described HEAD. There was nowhere to say what BASE was
supposed to do differently and nowhere to commit to an interface, so the schema could not
express a differential hypothesis and the agent was never asked for one. A claim like "a new
helper reports line terminators" validated perfectly while being untestable by construction.

**Change.** Five fields, added to the existing model call rather than a new one:

| Field | Meaning |
|---|---|
| `observable_operation` | the public call whose result changes |
| `trigger_condition` | the precondition that makes it change |
| `expected_head_observation` | what HEAD produces |
| `expected_base_hypothesis` | what BASE is believed to produce instead |
| `shared_interface` | the interface the experiment runs through |

**A separate plan-then-code call was considered and rejected.** Structured output already
forces the decomposition; a second call would double latency and the call budget without
adding a single constraint.

`shared_interface` earns its place mechanically rather than as prose: `_validate_grounding`
checks it against the Stage 3 partition, so a claim aimed at a HEAD-only symbol is rejected
*at selection time* instead of consuming all three candidate attempts. The check is skipped
when the partition is empty, so a wholly new module behaves as before.

`expected_base_hypothesis` gives the assessor a prediction to judge against. The assessor
now evaluates relatedness against the hypothesis fields rather than summary prose, and is
told explicitly that a test distinguishing the revisions *for an unrelated reason* is
`UNCERTAIN` even when the mechanical evidence is discriminating. **That narrows what may be
called support; it does not widen it.**

## Deliberately not implemented

| Idea | Why not |
|---|---|
| More repair attempts | Three is right. Eight repairs failed because feedback was blind, not because there were too few. |
| Repair role specialization (13.7) | Premature. Fix feedback, re-measure, then decide from data. Hard-coded roles would overfit to the known ten. |
| A separate planner model call (13.4) | Solved more cheaply by schema fields. |
| Multi-agent decomposition | ADR-002 is right. |
| Embedding retrieval over the repository | The call graph is deterministic and free. |
| Widening `MechanicalEvidenceClassifier`'s admissible set | Never. The only change made there was *adding* a status. |

## Invariant verification

`tests/test_evidence_gate_invariants.py` exists to be read by a skeptic. It pins, against
public mechanical surfaces:

1. **Exhaustively over all 13x13 status pairs**, that `BASE_ASSERTION_FAILED_HEAD_PASSED` is
   produced by exactly one pair and no other.
2. That `BASE TEST_ERROR + HEAD PASSED` is `NOT_COMPARABLE`, and that the Stage 2 rename did
   not promote it.
3. That the new status cannot reach a supporting outcome even when the model insists.
4. That a maximally confident model cannot manufacture support on any non-supporting pair.
5. That `UNCERTAIN` relatedness cannot support even a genuinely discriminating pair.
6. That the probed-install allowlist rejects package injection, remote requirements, shell
   metacharacters, and arbitrary interpreters.
7. That hand-written contracts keep the narrow literal allowlist.
8. That the attempt budget is still one initial plus two repairs.

## Status

| Stage | State | Tests |
|---|---|---|
| 0 — reasoning budgets | Complete | `test_reasoning_budget.py` |
| 1 — environment reach | Complete | `test_pytest_isolation.py`, `test_install_strategy.py`, `test_environment_introspection.py` |
| 2 — feedback, honest status, guardrails | Complete | `test_evidence_workflow.py`, `test_evidence.py`, `test_exception_observable_guardrails.py` |
| 3 — cross-revision grounding | Complete | `test_cross_revision_grounding.py` |
| 4 — differential claim schema | Complete | `test_claim_agent.py`, `test_cross_revision_grounding.py` |
| Invariants | Complete | `test_evidence_gate_invariants.py` |
| **New holdout run** | **Not run** | see `docs/20_EVALUATION_PROTOCOL_V2.md` |

**No performance claim is made anywhere in this branch.** Until the protocol in
`docs/20_EVALUATION_PROTOCOL_V2.md` has been executed once, the only honest statement is
that specific, named, reproduced defects have been fixed and pinned by regression tests.
