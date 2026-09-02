# What this branch changed, and why — read this first

A single-page guide to `feat/generalization-hardening` (PR #3). Everything here is
verifiable from the repository; nothing depends on trusting a summary.

**Start here, then follow the pointers.** The three deep documents are
[`18_INDEPENDENT_ARCHITECTURE_REVIEW.md`](18_INDEPENDENT_ARCHITECTURE_REVIEW.md) (the
diagnosis), [`19_GENERALIZATION_HARDENING_PLAN.md`](19_GENERALIZATION_HARDENING_PLAN.md)
(the change log), and [`20_EVALUATION_PROTOCOL_V2.md`](20_EVALUATION_PROTOCOL_V2.md) (what
to run next).

---

## The one-paragraph version

PatchProof's sealed unseen holdout supported 2 of 10 cases. The prior diagnosis attributed
most of that to agent reasoning. **Re-reading the code and the sealed artifacts says
otherwise: roughly 5.5 of the 8 failures were execution-harness failures, and two of the
three harness causes were still present in production code** at `e71052fb` — including in
the commit (`7404349`) that was believed to have fixed the environment. This branch fixes
the reachability problems, gives the repair loop information it never had, and grounds the
experiment on interfaces that exist on both revisions. **It makes no claim that PatchProof
now performs better, because no new evaluation has been run.**

---

## What was actually wrong

Each row was verified by reading the named file, not inferred.

| # | Defect | Where | Consequence in the holdout |
|---|---|---|---|
| 1 | Oracles ran **by absolute path from outside the checkout**; candidates were injected **into** it — different pytest rootdirs, different `conftest.py`, different `addopts` | `benchmarks/holdout/oracle_gate.json` → `pytest_execution_policy` | The oracle gate certified 10 cases as runnable when 8 could not execute a candidate at all |
| 2 | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` on every child process, and the argv never neutralized repository `addopts` | `execution_runtime.py:66` | pytest aborted during **argument parsing** → `PROCESS_ERROR`. This is anyio + cattrs. Installing dependencies does not help, because autoload suppression stops the installed plugins registering |
| 3 | Install allowlist admitted only `uv sync --frozen`, requiring a committed `uv.lock` at **both** revisions | `execution_contract.py:14` | No holdout repository qualifies. Dependency installation had only ever run against PatchProof's own repo |
| 4 | Repair feedback dropped every observed value unless status was `TEST_ERROR` | `evidence_workflow.py:673` | A repair after `BOTH_PASSED` was told its input didn't discriminate — and nothing else. 8 repairs, 0 discriminations |
| 5 | Signature grounding and all snippets came from **HEAD only** | `context_retrieval.py:568`, `:809` | An added helper was shown to the agent with no way to ask whether BASE has it. The Rich failure was structurally guaranteed |
| 6 | Reference snippets = one matched line ±2, then `break` | `context_retrieval.py:1000` | jsonschema turned on logic inside an unchanged helper whose body the agent never saw |
| 7 | Claim schema had **no field describing BASE** | `claim_agent.py` | The schema could not express a differential hypothesis, so the agent was never asked for one |
| 8 | `TEST_ERROR` classified as `ENVIRONMENTAL` | `evidence.py:23` | Starlette and Rich were counted as infrastructure noise, and the repair was told its environment was broken when the experiment had actually worked |
| 9 | Up to **8 cold installs per PR**, sharing one timeout with the test | `challenge.py:35`, `:62` | On a real repository the *install*, not the test, is what times out |

### Revised failure ledger

| Case | Previously called | Actually |
|---|---|---|
| dateutil, jinja, platformdirs | harness | harness — install disabled |
| anyio, cattrs | harness (missing plugins) | harness — **plugin autoload + `addopts`, still live in prod** |
| Starlette | reasoning | **harness classification** + reasoning |
| Rich | reasoning | reasoning — cross-revision grounding |
| jsonschema | reasoning (trigger synthesis) | reasoning — **context insufficiency** |
| more-itertools, packaging | supported | supported |

**≈5.5 harness · ≈2.5 reasoning · 2 supported.**

---

## What changed

| Stage | Commit | Change |
|---|---|---|
| **0** | `e4a559d` | `reasoning_budget.py`. All three agents hard-coded `ThinkingLevel.LOW`; the holdout averaged ~207 output tokens per call. Claim and candidate → MEDIUM. **The assessor deliberately stays LOW** — extra deliberation there cannot widen what the mechanical layer permits, so it is a false-positive risk, not a benefit |
| **1** | `d642747`, `3115a38` | Named-plugin disabling + `-o addopts=`; deterministic install prober (4 strategies from committed metadata); readiness gate that injects a probe test through the *identical* path a candidate uses; one worktree session per run; import grounding read from the prepared environment |
| **2** | `34159db` | Assertion diffs + deepest non-generated traceback frame in repair feedback, with value-preserving sanitization; `UNCAUGHT_EXCEPTION_ON_ONE_REVISION`; general exception→observable repair strategy with three new mechanical rejections |
| **3** | `4d08908` | `CrossRevisionInterfaces` partition; `HEAD_ONLY_INTERFACE` rejection; one-hop called-helper bodies |
| **4** | `d6f0d46` | Five differential claim fields, incl. `expected_base_hypothesis` and `shared_interface` — the latter validated against the Stage 3 partition |

### Two design points worth arguing with

**Widening the install allowlist does not weaken ADR-004.** No model chooses, proposes,
influences, or observes an install command. Commands are assembled from fixed templates
with exactly two variable slots — a committed requirements path, and a repository-declared
extra or PEP 735 group name — both validated against an allowlist before execution. Extras
are never invented. A hand-written `.patchproof.yaml` keeps the *old narrow* allowlist and
cannot use probed commands.

**Teaching the exception→observable transformation is only safe because its degenerate
forms are now mechanically impossible**, not merely discouraged in a prompt:
`BROAD_EXCEPTION_HANDLER` (bare `except`, `Exception`, `BaseException`, `pytest.raises`
with either), `UNGROUNDED_EXCEPTION_TYPE`, `SWALLOWED_OUTCOME`.

---

## What was deliberately *not* done

| Idea | Why not |
|---|---|
| More repair attempts | Three is right. 8 repairs failed because feedback was blind, not because there were too few |
| Repair role specialization | Premature — fix feedback, re-measure, then decide from data |
| A separate planner model call | Solved more cheaply by schema fields on the existing call |
| Multi-agent decomposition | ADR-002 is right |
| Embedding retrieval | The call graph is deterministic and free |
| Widening the admissible evidence set | **Never.** The only change was *adding* a status |
| Any repo-specific or case-specific branching | Explicitly forbidden; none exists in `src/` |

---

## Nothing here widened what counts as support

[`tests/test_evidence_gate_invariants.py`](../tests/test_evidence_gate_invariants.py) is
written to be read by a skeptic. It pins:

1. **Exhaustively across all 13×13 execution-status pairs** — `BASE_ASSERTION_FAILED_HEAD_PASSED`
   is produced by exactly one pair and no other
2. `BASE TEST_ERROR + HEAD PASSED` is `NOT_COMPARABLE`, and the Stage 2 rename did not
   promote it
3. The new status cannot reach a supporting outcome even when the model insists with full
   confidence
4. A maximally confident model cannot manufacture support on any non-supporting pair
5. `UNCERTAIN` relatedness cannot support even a genuinely discriminating pair
6. The probed-install allowlist rejects package injection, remote requirements, shell
   metacharacters, and arbitrary interpreters
7. Hand-written contracts keep the narrow literal allowlist
8. The attempt budget is still one initial + two repairs

**`benchmarks/` is byte-for-byte unmodified.** Every sealed manifest, journal, oracle hash,
raw result, and V1–V5 artifact is preserved. Verify with:

```bash
git diff --stat e71052fb..HEAD -- benchmarks/   # returns nothing
```

---

## Verify it yourself

```bash
uv sync --frozen --all-groups
uv run python -m pytest -q          # 387 passed, 1 skipped
uv run ruff check src tests         # clean
uv run ruff format --check src tests
uv build --wheel                    # builds
```

**Prove the Stage 1a fix is real** — delete the two `addopts` tokens from
`PYTEST_ISOLATION_ARGUMENTS` in `pytest_runner.py` and run:

```bash
uv run python -m pytest -q tests/test_pytest_isolation.py
```

It reports `BASE=PROCESS_ERROR, HEAD=PROCESS_ERROR` — reproducing the anyio/cattrs holdout
failure exactly. Restore the tokens and the same pair discriminates normally.

---

## No performance claim is made

**No new holdout was run. No live model call was made.** The only honest statement this
branch supports is that specific, named, reproduced defects are fixed and pinned by
regression tests.

[`20_EVALUATION_PROTOCOL_V2.md`](20_EVALUATION_PROTOCOL_V2.md) designs what must happen
before any generalization claim. Its most important correction is independent of any code
here:

> **Every v1 holdout case contains a real behavioral regression.** The false-support rate
> was therefore never measured under conditions where a false support was *possible*. "Zero
> incorrect supports on ten true-positive cases" is close to uninformative — and it is the
> first thing a skeptical reviewer will say.

A **negative-control set** — merged PRs with no observable behavior change, where the
correct answer is abstention — is now a required half of the protocol. One incorrect
support there would be a more serious result than a low support proportion on the positive
set.

Pre-registered target: **≥6/10 equivalent supported, zero incorrect supports on both the
positive and negative sets, and a published gate-rejection rate.** 8/10 is a stretch
target, not a commitment.

---

## Known gaps and open risks

**The install prober has never run against a real third-party repository.** Its tests use
fake file readers, and the pytest-isolation tests stub out the actual install. So
`uv pip install -e .[test]` against real jsonschema or real Rich is **untested end-to-end**.
This is the highest-risk part of the branch and the part most likely to need iteration.
Everything else is exercised against real git repositories and real pytest processes.

**The `HEAD_ONLY_INTERFACE` check can false-positive.** It matches attribute names against
the HEAD-only symbol set, so an unrelated object with a colliding method name would be
rejected. This is deliberate: the cost is one wasted attempt with actionable feedback, and
the check can only *reduce* support, never manufacture it.

**Stage effects are unmeasured.** Stage 0 was built as a control condition precisely so a
per-stage ablation over the known ten can attribute effects honestly. That ablation has not
been run.

---

## Reading order

| Time | Read |
|---|---|
| 5 min | This file |
| 20 min | `18_INDEPENDENT_ARCHITECTURE_REVIEW.md` §B (why the 2/10) and §C (ranked bottlenecks) |
| 45 min | + `tests/test_evidence_gate_invariants.py`, then `20_EVALUATION_PROTOCOL_V2.md` §2 |
| Full | + `19_GENERALIZATION_HARDENING_PLAN.md`, `tests/test_pytest_isolation.py`, `src/patchproof/install_strategy.py` |

`18_INDEPENDENT_ARCHITECTURE_REVIEW.md` also carries four appendices: a point-by-point
verdict on every proposed idea, a change/risk/cost table, an evidence index mapping every
factual claim to its file and line, and a checklist-answer index.
