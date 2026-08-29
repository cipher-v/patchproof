# Evaluation protocol v2 — design for the next sealed holdout

**Status: designed, not executed.** Nothing in this document reports a result. It
specifies what must be run, and under what constraints, before any claim about
generalization may be made about the work on `feat/generalization-hardening`.

The v1 protocol is preserved unchanged in `benchmarks/holdout/HOLDOUT_PROTOCOL.md` and
`docs/15`–`docs/17`. This document does not supersede it as history; it corrects it as
methodology.

---

## 1. The defect that invalidated v1's environment assumption

`benchmarks/holdout/oracle_gate.json` records:

> `"pytest_execution_policy": "The same oracle file in benchmarks/holdout/oracles was passed by absolute path to pytest for BASE and HEAD; no oracle copy was placed in either checkout."`

Generated candidates were injected *into* the checkout and executed by
`PytestRunner`. So the oracle path and the candidate path were **different pytest
invocations against different configuration roots**, with different rootdirs, different
`conftest.py` visibility, and different `addopts`. The oracle gate could therefore certify
ten cases as runnable while eight of them could not execute a generated candidate at all.

This is subtler than "the oracle used stubs". The two paths did not differ only in
dependencies; they differed in *which pytest configuration applied*.

### Rule 1 — one execution path, no exceptions

The oracle **must** be executed through byte-identical machinery to a generated candidate:
same injection point inside the checkout, same `PYTEST_ISOLATION_ARGUMENTS`, same
`ChildProcessEnvironmentPolicy`, same install plan, same `ChallengeSession`.

Concretely, oracle verification must call `ChallengeSession.run()` with the oracle bytes as
a `TestArtifact`. Any oracle-verification code path that constructs its own pytest
invocation is a protocol violation and must fail the gate.

---

## 2. The gap that makes "0 incorrect supports" nearly uninformative

Every one of the v1 cases contains a real behavioral regression. The false-support rate was
therefore never measured under any condition where a false support was *possible*. "Zero
incorrect supports on ten true-positive cases" is close to uninformative, and it is the
first thing a skeptical reviewer will say.

### Rule 2 — negative controls are a required, first-class half of the evaluation

A negative-control set of **8–10 merged pull requests with no externally observable
behavioral change**:

| Category | Example shape |
|---|---|
| Pure refactor | extract method, rename internal symbol, reorder branches |
| Typing only | add annotations, add a `.pyi`, `TYPE_CHECKING` imports |
| Documentation | docstrings, comments, README |
| Formatting | linter/formatter application |
| Test-only | add or restructure tests without touching production code |
| Dependency bump | lockfile or constraint change with no behavior delta |

**Correct behavior on every one is abstention.** Report `false_support_rate` on this set as
a headline number *beside* the support proportion. This is where conservatism becomes
demonstrated rather than asserted, and it is the strongest available evidence that the
architecture is right.

A single incorrect support here is a more serious result than a low support proportion on
the positive set.

---

## 3. Rule 3 — the readiness gate runs before sealing, and its rejections are published

A case is rejected **before** it enters the evaluation if, through the real path:

- the deterministic install probe returns `UNSUPPORTED`;
- BASE and HEAD imply different install strategies;
- either revision fails its install commands;
- either revision fails the injected readiness probe.

The rejection rate must be published with the results. Concealing it turns the support
proportion into a selection artifact: the gate systematically excludes the messiest
repositories, which biases the surviving set *easier*.

Report: `cases_considered`, `cases_rejected_at_gate`, `rejection_reasons`, `cases_sealed`.

---

## 4. Rule 4 — size the set so the headline number means something

At **n = 10**, the difference between 6 and 8 supported cases is one or two cases and is
not distinguishable from noise. The positive set must be **15–20 cases**, and the reported
number must carry that caveat explicitly rather than in a footnote.

---

## 5. Rule 5 — the old ten are diagnostic, never evidence of generalization

The v1 cases are now known development evidence. They may be used for **causal attribution
only**, and any such run must be labelled as an ablation, never as a generalization result.

### Permitted: staged ablation

Run each stage cumulatively over the old ten to attribute effects:

| Configuration | Question it answers |
|---|---|
| `e71052fb` (frozen v1) | baseline, already recorded |
| + Stage 0 | how much was a reasoning-budget artifact? |
| + Stage 1 | how much was harness contamination? |
| + Stage 2 | how much was blind repair feedback? |
| + Stage 3 | how much was missing cross-revision grounding? |
| + Stage 4 | how much was the non-differential claim schema? |

Re-verifying the ten v1 oracles through the *new* single execution path is itself a
decisive result: cases that become runnable now and were not before are pure harness
recovery, and can be described precisely as such.

### Forbidden

- Tuning until the ten pass and calling that generalization.
- Any repository-specific, case-specific, or claim-text-specific branching in `src/`.
- Exposing oracle source, oracle behavior, or changed upstream tests to any agent.
- Selectively rerunning failed cases and reporting only good attempts.
- Removing abstentions from the denominator.
- Deleting, rewriting, or re-sealing any v1 artifact.

---

## 6. Case selection

- Genuinely unseen merged PRs; **none** from the v1 ten, and none from the four
  development repositories.
- Diverse repositories, diverse behavioral categories, diverse *install shapes* — the
  install-strategy distribution is now itself a dimension worth spreading across.
- Python 3.12 compatible; exact immutable BASE/HEAD SHAs.
- A real deterministic regression: independent oracle asserts-fails on BASE and passes on
  HEAD, verified through Rule 1's single path.
- Changed upstream tests excluded from every prompt-bound context section, as in v1.
- Selection and oracle validation invoke no PatchProof semantic agent.

---

## 7. Metrics

Everything v1 reported, plus:

| Metric | Why it is new |
|---|---|
| `false_support_rate` on negative controls | the missing half of the evaluation |
| `cases_rejected_at_gate` + reasons | prevents a selection artifact |
| `oracle_direction_agreement` | of supported cases, how many discriminate in the *same direction* as the hidden oracle |
| `reached_changed_code` | did the candidate's BASE execution enter the changed symbol (via coverage on the BASE run)? Separates "wrong trigger" from "wrong assertion" |
| `head_only_interface_rejections` | measures the Rich failure class directly |
| `uncaught_exception_on_one_revision` | measures the Starlette failure class directly, no longer hidden inside `ENVIRONMENTAL` |
| `install_strategy_distribution` | environment reach |
| `installs_per_run` | confirms the Stage 1d caching holds in practice |
| `discrimination_after_repair_1` / `_2` | tests whether Stage 2 feedback actually converts repairs |

Failure causes must be categorized as: context insufficiency, claim abstraction,
trigger synthesis, interface grounding, test construction, exception interpretation,
repair strategy, execution environment, model limitation, architecture limitation.

The headline is **not** to be described as "accuracy". It is a *claim-scoped scenario
support proportion*, over an environment-ready set, alongside a false-support rate.

---

## 8. Production/holdout asymmetry — state it first

In the holdout, changed upstream tests are excluded. In production they are not, and
`_likely_test_snippets` will feed the pull request's own new tests to the claim agent.
**Production is therefore systematically easier than this holdout.**

This should be stated prominently rather than discovered by a reviewer. It makes the
holdout number stronger, not weaker.

---

## 9. Pre-registration

Before the blind run, commit: the manifest and its hash, the oracle hashes, the negative
control set, the target, and the metric list. Then run **once**.

The pre-registered target, per `docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md` section D:

> **at least 6 of 10 equivalent (scaled to the chosen n) supported, with zero incorrect
> supports on the positive set, zero incorrect supports on the negative-control set, and a
> published gate rejection rate.**

8/10 is a stretch target, not a commitment. A result of 7 with defensible methodology is
worth more than 10 obtained by leakage or special-casing, and claiming 8 and landing 6 is
worse at any review than claiming 6 and landing 7.
