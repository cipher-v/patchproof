# PatchProof — Independent Technical Review

**Reviewed commit:** `e71052fb4d9e4f855e5719e2350f727fb794e6b8` (main HEAD at handoff)
**Repository:** https://github.com/cipher-v/patchproof
**Method:** Full static read of `src/patchproof/`, `benchmarks/`, `docs/`, plus the sealed holdout artifacts and oracle gate. **No model calls were made and the holdout was not re-run.** Every claim below is traceable to source or to an artifact already committed in the repo.

---

## Contents

- [A. What PatchProof is, and whether the idea is sound](#a-what-patchproof-actually-is-and-whether-the-idea-is-sound)
- [B. Independent explanation of the 2/10](#b-independent-explanation-of-the-210)
- [C. Ranked generalization bottlenecks](#c-ranked-generalization-bottlenecks)
- [D. Is ≥8/10 plausible?](#d-is-810-plausible)
- [E. Ordered implementation plan](#e-ordered-implementation-plan)
- [F. Evaluation plan](#f-evaluation-plan-that-would-survive-a-hostile-reviewer)
- [G. Things the team appears to have missed](#g-things-the-team-appears-to-have-missed)
- [Bottom line](#bottom-line)
- [Appendix A — Point-by-point verdict on §13](#appendix-a--point-by-point-verdict-on-13-of-the-brief)
- [Appendix B — Change summary (§12 format)](#appendix-b--change-summary-12-requested-format)
- [Appendix C — Evidence index (verified code locations)](#appendix-c--evidence-index-verified-code-locations)
- [Appendix D — Answers to the §18 checklist](#appendix-d--answers-to-the-18-checklist-in-one-place)

---

## A. What PatchProof actually is, and whether the idea is sound

**My description of it:** PatchProof is a *differential-experiment* agent. It refuses to accept any of the three cheap signals people normally accept about a PR (author claim, LLM opinion, HEAD-green test), and instead insists that the only admissible evidence is a single artifact of bytes that behaves differently on two immutable commits. The LLM is confined to two jobs — *what to test* and *is this test about the claim* — and is deliberately denied the ability to influence *how it runs*.

**Is the idea sound? Yes, and it is better than it looks from the README.** The core insight — that "BASE fails / HEAD passes on identical bytes" is a falsifiable, replayable, content-addressed fact, and that the LLM must never be on the path that produces that fact — is genuinely good. It is the correct inversion of "LLM writes a test." I would defend it in front of a skeptical engineer.

Two things in the codebase are better than the team is giving itself credit for:

- **`MechanicalEvidenceClassifier`** (`src/patchproof/evidence.py`) is disciplined. It checks node-ID identity, artifact-hash identity *and* before/after hashes, then only admits `{PASSED, ASSERTION_FAILED}` as comparable. `_validate_semantic_decision` (`evidence_workflow.py:764`) mechanically forbids the model from returning `CLAIM_SUPPORTED_FOR_SCENARIO` unless the mechanical pattern already permits it. **The model cannot upgrade evidence.** That is the right shape.
- **`candidate_behavior_fingerprint`** (`test_generation.py:474`) — AST-normalized with local-name canonicalization and transitive helper inclusion — is a genuinely non-trivial anti-no-op device. Most implementations would have done `hash(source)`.

**The one architectural criticism of the *idea*:** the design assumes the bottleneck is *trust*. Empirically the bottleneck is *reach*. You built an excellent lie-detector and attached it to an agent that cannot get to the scene of the crime. Every engineering point below is about reach.

---

## B. Independent explanation of the 2/10

The 6A/6B split in the handoff brief is right *in kind*. It is wrong *in detail*, and the details change what should be fixed.

### B.1 The harness contamination is worse than "dependency-light"

Verified in code:

```
benchmarks/holdout/orchestration.py → hard_mode._challenge() → hard_mode.py:586

    PytestRunner(
        contract=_contract(),
        python_executable=Path(sys.executable),        # PatchProof's OWN venv
        install_dependencies=False,                    # nothing installed, ever
        repository_python_paths=case.repository_python_paths,   # PYTHONPATH='.' or 'src'
    )
```

So the ten candidate tests ran inside PatchProof's interpreter with the target repo's source bolted on via `PYTHONPATH`. That is the `six` / `markupsafe` story, confirmed.

But now read the committed oracle gate, `benchmarks/holdout/oracle_gate.json`:

> `"pytest_execution_policy": "The same oracle file in benchmarks/holdout/oracles was passed by absolute path to pytest for BASE and HEAD; no oracle copy was placed in either checkout."`

**The oracles and the candidates did not merely have different dependency sets. They had a different pytest rootdir.** An oracle invoked by absolute path from *outside* the checkout does not pick up the repository's `pyproject.toml [tool.pytest.ini_options] addopts`, its `conftest.py`, or its plugin requirements. A candidate injected *into* `patchproof_generated_tests/` inside the checkout picks up all three.

That is the true reason the oracle gate certified ten cases as runnable while the agent could not run in eight of them.

> §14 of the brief says "do not repeat the old mistake where the oracle was runnable using custom stubs but generated candidates used a weaker environment." It is more precise than that: **the oracle path and the candidate path were different pytest invocations against different configuration roots.** The new evaluation must run the oracle through *byte-identical machinery to the candidate path* — same injection point, same argv, same env — or it will lie again in a new way.

### B.2 A third harness defect that is still live in production

`execution_runtime.py:66` sets, unconditionally, for every child process:

```python
"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
```

and `pytest_runner.py` builds its argv with `-p no:cacheprovider --color=no --tb=short --maxfail=1 -q -rxX -o junit_family=xunit2` — and **never neutralizes the repository's own `addopts`**.

Consequence: for any repo whose `addopts` references a plugin option (cattrs' `--benchmark-*`; anyio's `anyio_mode`; anything requiring `-p`, `--cov`, or `--strict-markers` with plugin markers), pytest exits with a **usage error** before it ever writes a JUnit file. `PytestJUnitParser._without_report` then returns `PROCESS_ERROR`.

Check the sealed results: **anyio and cattrs are `PROCESS_ERROR`, not `COLLECTION_ERROR`.** That is the signature of argument parsing failing, not of a missing module. So the anyio failure is probably **not** "pytest_mock was missing" — it is the plugin-autoload/addopts collision, and **commit `7404349` did not fix it.** `install_dependencies=True` installs the plugins; `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` then refuses to load them anyway.

This is a two-line fix (explicit plugin control, and `-o addopts=`) and it is probably worth more than anything in §13 of the brief.

### B.3 The environment problem is not solved. It is scoped away.

`execution_contract.py:14`:

```python
_ALLOWED_INSTALL_COMMANDS = {
    ("uv", "sync", "--frozen"),
    ("uv", "sync", "--frozen", "--all-groups"),
}
```

That is the entire universe of installs PatchProof can perform. `uv sync --frozen` requires a committed `uv.lock` **at both BASE and HEAD**, and `EvidenceWorkflow.execute` additionally refuses to proceed unless the BASE and HEAD `.patchproof.yaml` are byte-identical.

None of the ten holdout repositories ship a `uv.lock` at those SHAs. Neither do most Python repos on GitHub today. So this sentence from the brief —

> "This was through the actual deployed production path with repository dependency installation, not the old dependency-light historical holdout runner."

— is true only because PR #2 was against *PatchProof itself*, the one repository in the world guaranteed to satisfy the contract. **The production path has never installed dependencies for a repository it did not author.** That is a demo on a repo designed around the harness.

Under §14's readiness gate, a new 10–20 case holdout of diverse real repos would reject nearly every case at the gate, and you would report an honest "0/0". That is the trap currently ahead.

### B.4 Two of the seven "environmental" failures were not environmental

`evidence.py:23` puts `TEST_ERROR` in `_ENVIRONMENTAL_STATUSES`. But an `IndexError` escaping from `starlette.datastructures.URL.replace` is not an environment problem — it is *the bug*, observed, on BASE, by the agent's own test.

The mechanical refusal to score it as support is **correct and should not be touched.** The *label* is a category error, and it has two costs:

- **Metrics.** The summary says "seven environmental terminal cases," which reads to any evaluator as infrastructure noise. Two of them (starlette, rich) were the agent successfully reaching the bug and the harness discarding it.
- **Repair.** The repair agent is told `mechanical_status=ENVIRONMENTAL`. That is actively misleading — it says "your environment is broken," when the correct message is "your experiment worked but produced an inadmissible observation *shape*."

**Needed:** a distinct status, e.g. `UNCAUGHT_EXCEPTION_ON_ONE_REVISION`. Still non-supporting. Still not upgradeable to support. But truthfully labelled, correctly counted, and actionable in repair.

### B.5 Revised failure ledger

| Case | Brief's classification | My classification |
|---|---|---|
| dateutil, jinja, platformdirs | harness (missing deps) | harness — install disabled ✅ |
| anyio, cattrs | harness (missing plugins) | harness — **plugin autoload + `addopts`; still live in prod** |
| starlette | reasoning (exception→observable) | **harness classification** + reasoning (repair) |
| rich | reasoning (claim abstraction) | reasoning — cross-revision grounding ✅ |
| jsonschema | reasoning (trigger synthesis) | reasoning — **context insufficiency**, not trigger synthesis |
| more-itertools, packaging | supported | supported ✅ |

**≈5.5 harness, ≈2.5 reasoning, 2 supported.** The measurement was contaminated more than the brief assumes, and *fewer* of the failures are model-reasoning problems.

---

## C. Ranked generalization bottlenecks

### 1 — Execution-environment generality
*Blocks 5–7 of 10. Blocks the entire new holdout.*

Three sub-problems, all fixable, none requiring the model:

**(a) The install allowlist is uv-only.** Needed: a small set of allowlisted, argv-only, model-inaccessible install *strategies* — e.g. `uv sync --frozen`, `uv pip install -e .[test]`, `pip install -e .`, `pip install -r <declared-path>` — chosen by a **deterministic prober** that inspects committed files (`uv.lock` → `pyproject.toml` extras → `setup.py` → `requirements*.txt`) at both revisions. The model still never sees or proposes a command; the deterministic vocabulary is simply wider. **ADR-004's actual invariant ("no model-generated commands") is fully preserved.**

**(b) `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` must go**, or become per-repo, and the fixed argv must carry `-o addopts=` so the repository's own opinions cannot break a single-node run.

**(c) `prepare_environment` currently runs installs and stops.** It never proves pytest can collect and run anything. It must end by injecting a trivial always-passing probe test through the *identical* injection + argv path and requiring `PASSED` on both revisions. That is the only readiness gate that means anything — and it is the same gate §14 needs.

Also: worktrees are created and destroyed per call (`challenge.py:35` and `:62`), and `UV_NO_CACHE=1` is set alongside `UV_CACHE_DIR` — so installs run cold, **up to 8 times per PR**, each bounded by the same `timeout_seconds` (≤300) that also bounds the test. On real repos, the install *is* the timeout.

- **False-positive risk:** zero. Nothing here touches the evidence gate.
- **Cost:** 2–4 days.
- **Verdict:** do this first, unconditionally.

### 2 — Repair feedback is nearly information-free in exactly the cases where repair matters

`EvidenceWorkflow._execution_exception` (line 673):

```python
if result.status is not TestExecutionStatus.TEST_ERROR or not result.detail:
    return None
```

For `BOTH_PASSED` and `BOTH_ASSERTION_FAILED` — the two situations where a repair could actually learn something — the model receives, verbatim:

```
BASE status=PASSED; generated_line=7:assert validator.is_valid([1])
HEAD status=PASSED; generated_line=7:assert validator.is_valid([1])
mechanical_status=NON_DISCRIMINATING; pattern=BOTH_PASSED
```

**It never sees a single observed value.** The jsonschema repair was asked to find a discriminating input while being told nothing except "your input wasn't discriminating." That is not a reasoning failure; that is a blindfold. **Eight repairs producing zero discriminations is the predicted outcome of this feedback design, not evidence about Gemini.**

The fix is general and cheap: extract the pytest assertion-diff block (`E   assert ... == ...`, the `left`/`right` lines) from `result.detail`, sanitize, and pass both revisions' versions. Ideally also surface the **first traceback frame inside repository production code** (path + line) — which tells the model whether its trigger even *reached* the changed function, the single most valuable bit for `BOTH_PASSED`.

> ⚠️ Caution: `_sanitize_feedback_text` currently runs `_UNIX_PATH_PATTERN` and `_OPAQUE_VALUE_PATTERN` over everything, which will shred file paths and long literals in an assertion diff. Sanitization must become value-preserving here or the fix does nothing.

- **False-positive risk:** low but non-zero — better feedback → more discriminating tests → the semantic assessor gates more decisions. Mitigate via #4, not by degrading feedback.
- **Cost:** 1–2 days.
- **Verdict:** highest reasoning-side leverage per hour.

### 3 — No cross-revision interface grounding, and no BASE-side context at all

`retrieve_callable_signatures(head_sha=..., ...)` — **HEAD only**, capped at `max_signature_count=8` and `max_signature_context_chars=2_000`, and never used to check whether a symbol *exists* on BASE. Snippets: **HEAD only** (`_changed_python_context` reads BASE only for *deleted* files). The model's entire view of BASE is three lines of unified diff context (`--unified=3` in `_bounded_diff`).

Two failures follow structurally:

**Rich.** The agent saw `Segment.split_lines_terminator` in the HEAD snippet, had no mechanism to ask "does this exist on BASE?", and tested it. Given the architecture, this outcome was *guaranteed*, not a lapse of judgment.

The fix is deterministic, not prompt-based: compute symbol sets on both revisions and hand the claim/candidate agent an explicit partition — `interfaces_present_on_both`, `interfaces_new_on_head`, `interfaces_removed_on_base` — plus a hard validator rule that a candidate's *top-level imported* names must resolve on BASE. That single validator turns Rich-class failures from silent waste into a rejection with actionable feedback, and generalizes to every "PR adds a helper" PR, which is most of them.

**jsonschema.** `_reference_snippets` (line 1000) emits a **single matched line ±2** per referencing file, then `break`s. A five-line window. The nested-container equality semantics live in an unchanged helper whose body the agent never saw. This is **context insufficiency**, not trigger-synthesis weakness. §13.3 is the right diagnosis and should rank above §13.4.

Bounded fix for §13.3: from the changed symbols' ASTs, collect *directly called* one-hop callees defined in the repo and include each callee's full body up to a per-callee char cap, ranked above generic name-match references. Budget it (say 3 callees × 600 chars) and drop the low-value one-line reference snippets to pay for it. **Do not** use embedding retrieval — the call graph is deterministic and free.

- **False-positive risk:** essentially zero (grounding strictly *narrows* what may be tested).
- **Cost:** 3–4 days.

### 4 — The claim schema has no BASE contrast and no shared-interface commitment

`BehavioralClaimDraft` has `summary / preconditions / action / expected_behavior / affected_symbols / supporting_context / confidence`. **Every field describes HEAD.** There is no place to state what BASE was supposed to do differently, and no place to commit to an interface. The schema literally lacks room for a falsifiable differential hypothesis, so the agent is not being asked for one.

§13.1 is correct. Implement it as **added fields on the existing claim output**, not a new model call:

| Field | Meaning |
|---|---|
| `observable_operation` | the public call whose result changes |
| `trigger_condition` | the precondition that makes it change |
| `expected_head_observation` | what HEAD produces |
| `expected_base_hypothesis` | what BASE is believed to produce instead |
| `shared_interface` | path + qualname, validated against `interfaces_present_on_both` |

The value is not the prose — it is that `shared_interface` becomes **deterministically checkable** against #3, and `expected_base_hypothesis` gives the mechanical layer a *prediction to check*, which is what makes this an experiment rather than a guess.

On §13.4: **do not add a separate plan→code model call.** It doubles latency and call budget to solve a problem that extra output fields on an existing call already solve. Structured output already forces the decomposition.

- **False-positive risk:** low, and it may *reduce* them — `expected_base_hypothesis` gives the semantic assessor a far better relatedness test than it has now.
- **Cost:** 2–3 days.

### 5 — Reasoning budget is throttled below the difficulty of the task

All three agents (`adk_claim_agent.py`, `adk_test_agent.py`, `adk_evidence_assessor.py`) run with:

```python
thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
temperature=0.1
include_contents="none"
```

and the claim agent caps at `max_output_tokens=2_048` (the candidate agent gets 12,000, the assessor 4,096). `include_contents="none"` also means each invocation is fully stateless — there is no carry-over of the agent's own prior reasoning between the initial candidate and its repairs; the repair sees only the previous *source* plus the bounded feedback described in bottleneck #2.

The sealed holdout accounting: **6,205 output tokens across 30 calls ≈ 207 output tokens per call**; 128,767 tokens total for ten PRs. The model is being asked to reverse-engineer a behavioral trigger from a diff at roughly two hundred tokens of budget, with thinking at minimum.

Before concluding anything about model capability, raise thinking to at least MEDIUM on the claim and repair paths and raise the claim output cap. This is a config change. At these volumes the cost delta is negligible and the latency delta is seconds. **If it moves results on its own, several "reasoning failures" were budget failures** — which changes how the project should be described.

- **False-positive risk:** low. Mechanical gates unchanged.
- **Cost:** hours.

### 6 — Exception→observable transformation (§13.5)

Genuinely general and worth building, but it is **#6, not #1** — it fixes exactly one of the ten cases, and #1 must land first for it to matter at all.

The right general form is a **repair strategy**, deterministically selected when the mechanical layer sees `{TEST_ERROR, PASSED}` and the error's exception type is grounded in context:

```python
try:
    observed = ("ok", repr(operation()))
except SpecificException as error:      # named type only, never bare Exception
    observed = ("error", type(error).__name__)

assert observed == expected_head_observation
```

Two guardrails, both enforceable in `CandidateTestValidator`, both necessary:

1. Reject `except Exception`, `except BaseException`, bare `except`, and any handler whose type is not present in the deterministic context.
2. Reject a handler that swallows without recording (`pass`, `...`) — the pattern must bind the outcome to an asserted value.

This lets starlette-class cases become admissible **without weakening the gate**: `BASE=ASSERTION_FAILED, HEAD=PASSED` is produced by the test's own logic, not by reclassifying `TEST_ERROR`. §16's constraint is preserved exactly.

- **False-positive risk:** **moderate — the highest of anything recommended here.** A model that learns "wrap in try/except and assert the shape" can produce tests that discriminate on incidental exception differences. The guardrails plus the assertion-relatedness check are the mitigation.
- **Cost:** 2 days.
- **Verdict:** build it last, measure it separately.

### Things I would explicitly NOT do

- **More repair attempts.** Three is the right number. Eight repairs produced zero discriminations because feedback was empty, not because there were too few.
- **Repair role specialization (§13.7).** Premature. Fix feedback, then look at the data. Hard-coded role scripts will overfit to the ten.
- **A separate planner LLM call (§13.4 as a new call).** Solved more cheaply by schema fields.
- **Multi-agent decomposition.** ADR-002 is right; the instinct to keep it is right.
- **Embedding / semantic retrieval over the repo.** The call graph is deterministic — use it.
- **Anything that widens `MechanicalEvidenceClassifier`'s admissible-status set.** The only change wanted there is *adding a status*, never *widening what counts as support*.

---

## D. Is ≥8/10 plausible?

**Honest answer: not as a commitment. 6–8 is the realistic band; I forecast 6–7 on a genuinely fresh, diverse holdout.**

Reasoning:

- On the old ten, the ceiling is 10 (an oracle exists for each, so each is discriminable in principle).
- Fixing bottleneck #1 converts the five harness-blocked cases into *real attempts* — **not into successes**.
- Their conditional success rate is unknown. The only evidence about the agent's unassisted hit rate on cases it actually reached is **2 of 3 mechanically-comparable initial candidates**. On a sample of three. That is not extrapolatable.

Then apply the honest discounts:

- A *new* holdout of different repositories will be harder than a re-run of the old ten (now development evidence).
- §14's readiness gate will exclude some cases, and the excluded cases will systematically be the messy ones — biasing the remainder *easier*. **A good number there is partly an artifact of the gate, so the exclusion rate must be reported alongside it.**

**What I would commit to publicly:**

> ≥6/10 supported on a fresh environment-ready holdout, with **0 incorrect supports**, and a documented rejection rate at the readiness gate.

That is defensible and beatable. Claiming 8 and landing 6 with a footnote is much worse at a hackathon than claiming 6 and landing 7. The brief already says you'd rather have 7/10 with defensible methodology — hold that line.

---

## E. Ordered implementation plan

Sequenced so each stage is independently shippable and each one's effect is measurable in isolation. All work in a **new branch**. Nothing under `benchmarks/` is touched — the sealed artifacts are the most credible thing in the repository, precisely because they record a disappointing result honestly.

### Stage 0 — Reasoning budget (hours, free)
Raise `thinking_level` to MEDIUM on claim + candidate; raise claim `max_output_tokens`. Change nothing else.
*This is a control condition: run it before any other change so later gains can be attributed honestly.*

### Stage 1 — Environment (2–4 days) — the gate on everything
1. Deterministic install-strategy prober + widened argv allowlist (model still never sees commands).
2. Remove/scope `PYTEST_DISABLE_PLUGIN_AUTOLOAD`; add `-o addopts=` to the fixed argv.
3. `prepare_environment` ends with a trivial probe test through the *identical* injection+argv path, requiring PASS on both revisions.
4. Persist per-revision installed environments across the run instead of re-installing per worktree pair; split install timeout from test timeout.
5. Derive `_allowed_import_roots` from the **installed environment** plus repo top-level packages, not from snippet paths. (Alone, this unblocks the cattrs-class `UNGROUNDED_IMPORT` false negative.)

### Stage 2 — Feedback (1–2 days)
Assertion diffs on both revisions; first production-code traceback frame; value-preserving sanitization. Add `UNCAUGHT_EXCEPTION_ON_ONE_REVISION` as a distinct non-supporting status with its own repair message.

### Stage 3 — Grounding (3–4 days)
Cross-revision symbol partition; BASE-resolution validator on candidate imports; one-hop callee bodies in context (paid for by dropping one-line reference snippets).

### Stage 4 — Claim schema (2–3 days)
The five new fields; `shared_interface` validated against Stage 3's partition; assessor prompt uses `expected_base_hypothesis` for relatedness.

### Stage 5 — Exception→observable repair (2 days)
With both validator guardrails. Measure separately; the only change with meaningful false-positive exposure.

**Deadline read:** Stages 0–2 are comfortably pre-deadline and hold most of the value. Stages 3–4 are what make the *story* good ("the agent grounds its experiment on a shared interface across revisions") — high demo value, real engineering. Stage 5 is optional if time runs short.

---

## F. Evaluation plan that would survive a hostile reviewer

The current evaluation has one structural hole more serious than anything in the agent:

> **Every one of the ten cases contains a real behavioral regression. The false-positive rate has therefore never been measured under any condition where a false positive was possible.** "0 incorrect supports" on ten true-positive cases is close to uninformative. It is the first thing a skeptical engineer will say.

**1. Negative controls — the missing half.**
8–10 merged PRs with *no* externally observable behavior change: pure refactors, renames, type annotations, docstrings, dependency bumps, test-only changes, formatting. Correct behavior is **abstention** on all of them. Report `false_support_rate` on this set as a headline number beside the support proportion. This is where the conservatism becomes *demonstrated* rather than *asserted* — and it is the strongest available evidence that the design is right.

**2. Identical-path oracle gating.**
The oracle must be injected into the checkout and executed through byte-identical machinery to the candidate path — same file placement, same argv, same env policy, same install. If the §B.1 fix is correct, re-verifying the ten old oracles through the new path is itself a decisive result: cases that pass now and didn't before are pure harness recovery, and can be described as such precisely.

**3. Report the readiness-gate rejection rate.**
Cases excluded / cases attempted, with reasons. Concealing it turns the support proportion into a selection artifact.

**4. Stage-ablation on the old ten — for attribution only.**
Run Stage 0, then 0+1, then 0+1+2, etc. This is legitimate use of known development evidence *for causal attribution*, and is **not** a generalization claim. State that distinction explicitly in the write-up; making it is itself a mark of methodological seriousness.

**5. The fresh holdout — run exactly once, 15–20 cases.**
Not 10: at n=10 the difference between 6 and 8 is one or two cases and is not distinguishable from noise — say so. Diverse repos, diverse behavioral categories, diverse install shapes, changed upstream tests excluded, oracle hidden, no reruns, no post-hoc tuning. **Pre-register the target before running.**

**6. Metrics — §15's set, plus these:**

| Metric | Why |
|---|---|
| `oracle_direction_agreement` | of supported cases, how many discriminate in the same direction as the hidden oracle |
| `reached_changed_code` | did the candidate's execution enter the changed symbol (deterministically observable via `coverage`/`sys.settrace` on the BASE run)? Separates "wrong trigger" from "wrong assertion" |
| `shared_interface_violation_rate` | candidates rejected for using HEAD-only symbols — measures the Rich class directly |
| install-strategy distribution; mean installs per run | environment health and cost |

**7. Note the production/holdout asymmetry in the write-up.**
In holdout, changed upstream tests are excluded. In production they are not, and `_likely_test_snippets` will happily feed the PR's own new tests to the claim agent. **Production is therefore systematically easier than the holdout.** That is fine — it is the honest reading of the sealed result, it makes the holdout number *more* impressive, and saying it first is worth more than having it noticed.

---

## G. Things the team appears to have missed

1. **The oracle gate and the candidate path were different pytest invocations against different config roots** — not just different dependency sets. Root cause of the whole contaminated measurement, and it is stated in your own committed artifact.
2. **`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` is still in production** and defeats the dependency installation `7404349` added. The anyio/cattrs `PROCESS_ERROR`s point here, not at missing modules.
3. **The install allowlist admits only uv-lockfile repositories.** "Production dependency installation" has only ever been exercised against PatchProof itself. Largest gap between the project's self-description and its actual reach.
4. **Repair feedback contains no observed values** in `BOTH_PASSED`/`BOTH_ASSERTION_FAILED`. Eight failed repairs is a predicted consequence of the design.
5. **False positives have never been measured under conditions where one could occur.** Negative controls are the highest-value evaluation work available.
6. **`thinking_level=LOW` and ~207 output tokens/call.** You may be attributing to the model what you configured.
7. **`_allowed_import_roots` is derived from snippet paths, not the installed environment** — a deterministic false-negative generator (cattrs / `attrs`).
8. **Up to 8 cold `uv sync` runs per PR**, each sharing a ≤300s budget with the test. On any real repository, install *is* the timeout.
9. **`TEST_ERROR → ENVIRONMENTAL`** conflates "the environment is broken" with "the code under test raised" — miscounting metrics and misdirecting repair.

---

## Bottom line

The idea is sound and the deterministic core is the best part of the codebase — **do not touch its guarantees.**

The 2/10 is mostly a measurement artifact, and the artifact is **not fully repaired**: two of its three causes are still in production code.

**Fix reach before reasoning.** Stages 0–2 are cheap, carry near-zero false-positive risk, and should move the number more than every idea in §13 combined. Then do grounding and the claim schema — also the changes that make the demo narrative genuinely *agentic* rather than merely careful.

**Target: ≥6/10 with 0 incorrect supports *and* a clean negative-control set.** That pairing is a far stronger claim than 8/10 alone.

---

## Appendix A — Point-by-point verdict on §13 of the brief

| § | Idea | Verdict | Rationale |
|---|---|---|---|
| **13.1** | Behavior-first claim abstraction | **Implement** — as added fields on the existing claim output | The schema currently has *no* field describing BASE. The agent is not being asked for a differential hypothesis, so it doesn't produce one. Do not force speculation: allow the model to abstain on `expected_base_hypothesis` and treat abstention as a signal, not an error. |
| **13.2** | Cross-revision symbol/interface grounding | **Implement — highest-value reasoning fix** | Rich's failure was architecturally guaranteed, not a judgment lapse. Make it a *deterministic* partition + validator rule, never a prompt instruction. Fully general; zero false-positive risk. |
| **13.3** | Richer unchanged-helper semantics | **Implement — bounded one-hop callees only** | jsonschema is a context-insufficiency failure, not a trigger-synthesis failure. Current reference snippets are a 5-line window with a `break`. Budget 3 callees × ~600 chars; pay for it by dropping one-line reference snippets. **No embedding retrieval** — the call graph is deterministic and free. |
| **13.4** | Counterexample / trigger-synthesis plan | **Implement as schema fields; do NOT add a separate model call** | Structured output already forces the decomposition. A separate plan→code call doubles latency and call budget for no additional constraint. The `{operation, trigger, expected_head, expected_base_contrast, shared_interface, observable}` shape is right — it belongs in 13.1's fields. |
| **13.5** | Exception→observable transformation | **Implement last, measure separately** | Genuinely general and fixes starlette-class cases *without* touching the gate. But it is the only recommendation with **moderate** false-positive exposure. Requires two hard validator guardrails (no broad/ungrounded handlers; no swallowing without recording). |
| **13.6** | Better execution feedback | **Implement — highest leverage per hour** | Current feedback is not merely "bounded", it is empty of observed values in the two cases where repair matters. This is the difference between an informed revision and a re-roll. |
| **13.7** | Repair diversity / role specialization | **Do NOT implement yet** | Premature. Eight repairs failed because feedback was blind, not because roles were undifferentiated. Fix 13.6, re-measure, then decide from data. Hard-coded repair roles risk overfitting to the known ten. |
| **13.8** | Deterministic search before extra LLM calls | **Implement — this is 13.2 + 13.3 + the import-root fix** | The right framing. Symbol existence on BASE, signature conformance, and callee bodies are all statically derivable. Add `reached_changed_code` (coverage on the BASE run) as a deterministic diagnostic. Excluded upstream tests and oracle code remain out of bounds. |

---

## Appendix B — Change summary (§12 requested format)

| Change | Why it generalizes | False-positive risk | Cost | Pre-deadline? |
|---|---|---|---|---|
| **S0** Raise thinking level / claim output cap | Not repo-specific; removes an artificial reasoning ceiling on every PR | Low — mechanical gates unchanged | Hours | ✅ Yes |
| **S1a** Deterministic install-strategy prober + widened argv allowlist | Every Python repo declares deps in one of ~4 committed forms; probing is repo-agnostic | **Zero** — evidence gate untouched | 1–2 d | ✅ Yes |
| **S1b** Remove `PYTEST_DISABLE_PLUGIN_AUTOLOAD`; add `-o addopts=` | Neutralizes *any* repository's pytest configuration, not one repo's | **Zero** | Hours | ✅ Yes |
| **S1c** Readiness gate ends with a probe test on the real path | Proves runnability for any repo before spending model calls | **Zero** | 1 d | ✅ Yes |
| **S1d** Persist installed env; split install/test timeouts | Cost and reliability on all repos | **Zero** | 1 d | ✅ Yes |
| **S1e** `_allowed_import_roots` from installed env | Removes a systematic false-negative for every repo with third-party deps | **Zero** (it *removes* wrongful rejections) | 0.5 d | ✅ Yes |
| **S2** Assertion diffs + production traceback frame in feedback | Every repair benefits; nothing repo-specific | Low — more discriminating tests reach the assessor | 1–2 d | ✅ Yes |
| **S2b** `UNCAUGHT_EXCEPTION_ON_ONE_REVISION` status | Correct accounting + correct repair signal for all repos | **Zero** — still non-supporting, still non-upgradeable | 0.5 d | ✅ Yes |
| **S3** Cross-revision partition + BASE-resolution validator | Applies to every "PR adds a helper" PR — the most common shape | **Zero** — strictly narrows what may be tested | 2 d | ✅ Yes |
| **S3b** One-hop callee bodies in context | Diff-driven, budgeted, repo-agnostic | **Zero** | 1–2 d | ⚠️ Tight |
| **S4** Claim schema differential fields | Makes the hypothesis checkable rather than assertable | Low → may *reduce* FPs | 2–3 d | ⚠️ Tight |
| **S5** Exception→observable repair strategy | General transformation, not a case rule | **Moderate — highest of all** | 2 d | ⚠️ Optional |

---

## Appendix C — Evidence index (verified code locations)

Every factual claim in this review traces to one of these:

| Finding | Location |
|---|---|
| Holdout ran with no dependency installation, in PatchProof's own venv | `src/patchproof/hard_mode.py:586` (`install_dependencies=False`, `python_executable=Path(sys.executable)`) |
| Oracle executed by absolute path, outside the checkout | `benchmarks/holdout/oracle_gate.json` → `pytest_execution_policy` |
| Plugin autoload disabled for every child process | `src/patchproof/execution_runtime.py:66` |
| Repository `addopts` never neutralized | `src/patchproof/pytest_runner.py` — fixed argv lacks `-o addopts=` |
| Install allowlist is uv-only | `src/patchproof/execution_contract.py:14` (`_ALLOWED_INSTALL_COMMANDS`) |
| BASE/HEAD contracts must be byte-identical | `src/patchproof/evidence_workflow.py` — `"BASE and HEAD execution contracts differ"` |
| `TEST_ERROR` classified as environmental | `src/patchproof/evidence.py:23` (`_ENVIRONMENTAL_STATUSES`) |
| Repair feedback drops exception info unless `TEST_ERROR` | `src/patchproof/evidence_workflow.py:673` (`_execution_exception`) |
| Feedback sanitizer shreds paths and long literals | `src/patchproof/evidence_workflow.py` — `_UNIX_PATH_PATTERN`, `_OPAQUE_VALUE_PATTERN` |
| Signatures extracted from HEAD only, capped at 8 | `src/patchproof/context_retrieval.py:568` (`retrieve_callable_signatures`) |
| Snippets are HEAD-only except for deleted files | `src/patchproof/context_retrieval.py:809` (`_changed_python_context`) |
| Reference snippets are one line ±2, then `break` | `src/patchproof/context_retrieval.py:1000` (`_reference_snippets`) |
| Allowed import roots derived from snippet paths | `src/patchproof/test_generation.py` (`_allowed_import_roots`) |
| Thinking level LOW on all three agents | `adk_claim_agent.py`, `adk_test_agent.py`, `adk_evidence_assessor.py` |
| Worktrees recreated per call → repeated installs | `src/patchproof/challenge.py:35` and `:62` |
| `UV_NO_CACHE=1` set alongside `UV_CACHE_DIR` | `src/patchproof/execution_runtime.py` |
| Model cannot upgrade mechanical evidence | `src/patchproof/evidence_workflow.py:764` (`_validate_semantic_decision`) |
| AST behavior fingerprint blocks no-op repairs | `src/patchproof/test_generation.py:474` |
| Holdout token accounting (6,205 output / 30 calls) | `benchmarks/holdout/results/summary.json` → `usage` |
| anyio + cattrs were `PROCESS_ERROR`, not `COLLECTION_ERROR` | `benchmarks/holdout/results/summary.json` → `cases` |
| Holdout repos use `PYTHONPATH` only, no install | `benchmarks/holdout/manifest.json` → `repository_python_paths` |

---

## Appendix D — Answers to the §18 checklist, in one place

| Question asked | Answer |
|---|---|
| Your own explanation of PatchProof | §A — a differential-experiment agent; the LLM is kept off the evidence-producing path |
| Is the core product idea sound? | **Yes.** The BASE/HEAD identical-bytes gate is the right inversion of "LLM writes a test" |
| Does the architecture match the goal? | **Partially.** The evidence layer matches it well; the retrieval and feedback layers do not reach far enough to feed it |
| Independent explanation of the 2/10 | §B — ≈5.5 harness, ≈2.5 reasoning, 2 supported |
| Harness vs. genuine reasoning failures | §B.5 table |
| Top 3–5 generalization bottlenecks | §C — (1) environment generality, (2) blind repair feedback, (3) no cross-revision grounding / no BASE context, (4) claim schema has no BASE contrast, (5) throttled reasoning budget |
| Highest-leverage generalized improvements | Stages 0–2: thinking budget, environment reach, real feedback |
| Which ideas I would NOT implement | §C "Things I would explicitly NOT do" + Appendix A rows 13.4 (separate call) and 13.7 |
| Expected effect of each improvement | Appendix B |
| False-positive risk of each | Appendix B — all zero-to-low except S5 (moderate) |
| Is ≥8/10 plausible on a fresh holdout? | §D — **not as a commitment.** 6–8 band; forecast 6–7; commit publicly to ≥6 with 0 incorrect supports |
| Ordered implementation plan | §E |
| Evaluation plan for a skeptic | §F — negative controls are the missing half |
| What the team appears to have missed | §G — nine items |
