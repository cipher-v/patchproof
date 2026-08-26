# Hard-mode evaluation

## Status

PatchProof completed one declared blind run over five difficult cases using
`gemini-3.6-flash`. Four cases were historical merged pull requests from four public
repositories and one was a deterministic local synthetic repository. The run was not repeated,
filtered, or cherry-picked.

The observed result was:

- 5/5 hidden developer oracles independently reproduced `BASE=ASSERTION_FAILED` and
  `HEAD=PASSED` before any model call;
- 4/5 claims selected and mechanically grounded; the remaining claim call was blocked by the
  Gemini free-tier request quota;
- 2/5 initial candidates were statically valid;
- 1/5 initial candidates was executable and discriminating;
- 3 repairs were used: one became discriminating and two remained invalid structured output;
- 2/5 cases ended as `CLAIM_SUPPORTED_FOR_SCENARIO`;
- 2/5 ended with insufficient evidence after both candidate slots failed;
- 1/5 was not evaluated past claim selection because of the provider quota;
- 0 incorrect supports were observed relative to the hidden-oracle differential direction.

These are raw counts from a five-case adversarial diagnostic, not an accuracy estimate.

## Frozen protocol

The manifest is [`benchmarks/hard_mode/manifest.json`](../benchmarks/hard_mode/manifest.json). Its
SHA-256 for the declared run was
`166c00f2b6337239c38687f0ab622de8a54b46be20b8213a46b309061cd9e2d3`.

The protocol preserved the production boundaries:

1. one grounded claim call per case;
2. one initial candidate and at most one repair;
3. deterministic candidate validation;
4. exact candidate bytes executed on immutable BASE and HEAD worktrees;
5. mechanical classification as the authority;
6. semantic assessment only after discriminating evidence;
7. one transient provider retry at most, as already allowed by PatchProof;
8. no manual candidate edits and no selective reruns.

The live harness wrote an append-only journal before the first case. The presence of that journal
permanently blocks another invocation of the declared run. The checked-in raw result SHA-256 is
`5a1cfdbe29d913a024130acd7fd4bb4d7f2d8162ee2571220c342ec8ab01d163`.

## Anti-leakage design

Each historical case declares every changed Python test path. `DeterministicContextRetriever`
removes those exact paths before any prompt-bound operation: bounded diff generation, changed
symbol extraction, changed-test prioritization, likely-test scanning, import snippets, and reference
scanning. Rename source paths are excluded with their destination. Counts are retained for audit,
but excluded names and contents are not placed in the model context.

The independent oracle files live under `benchmarks/hard_mode/oracles/`, outside every evaluated
repository. The live code path never reads oracle bytes. Before opening the live gate, the harness:

- compared the complete changed Python test path set with the manifest exclusions;
- verified each oracle SHA-256 and its single declared top-level test;
- executed identical oracle bytes on BASE and HEAD;
- required `ASSERTION_FAILED/PASSED` and unchanged before/after artifact hashes;
- scanned serialized context for excluded paths and substantial exact oracle lines;
- stored a context SHA-256 and required live retrieval to reproduce it exactly.

The preserved gate is
[`benchmarks/hard_mode/results/oracle_gate.json`](../benchmarks/hard_mode/results/oracle_gate.json),
SHA-256 `a91a3aa05301a89ea1d0f49722a84bf5c7b42b1446e86eea195a7df02dce1975`.

An initial model-free preflight exposed optional upstream suite dependencies: attrs loaded Hypothesis
from `tests/conftest.py`, and marshmallow loaded `simplejson`. Generated artifacts were moved to the
top-level `patchproof_generated_tests/` directory. This kept the same pytest process, source-tree
imports, artifact rules, and classifier while preventing unrelated upstream suite fixtures from
loading. All five oracles then reproduced. This happened before the declared model run.

## Cases and oracle admission

| Case | Production change | Why it is harder | BASE oracle | HEAD oracle |
|---|---|---|---|---|
| [Click #3678](https://github.com/pallets/click/pull/3678) | `src/click/core.py`, approximately +44/-2 | Command construction, automatic help state, parser storage, callback binding, and runner output interact when a user parameter is named `help`. | `ASSERTION_FAILED` | `PASSED` |
| [attrs #1513](https://github.com/python-attrs/attrs/pull/1513) | `src/attr/validators.py`, +3/-1 | Two nested context managers must preserve an intermediate global state before final cleanup. | `ASSERTION_FAILED` | `PASSED` |
| [marshmallow #2903](https://github.com/marshmallow-code/marshmallow/pull/2903) | `src/marshmallow/schema.py`, +1/-1 | Two schemas, a nested required field, an aliased `data_key`, and dot-delimited partial paths interact. | `ASSERTION_FAILED` | `PASSED` |
| [Astroid #3075](https://github.com/pylint-dev/astroid/pull/3075) | `astroid/rebuilder.py`, +21/-16 | AST rebuilding and position extraction must degrade safely under a deterministic tokenizer exception. | `ASSERTION_FAILED` | `PASSED` |
| Local nested workspace | `workspace_registry.py`, +1/-1 | Three overlapping objects flow through containment filtering and depth-based ownership resolution. | `ASSERTION_FAILED` | `PASSED` |

All exercised interfaces existed on both revisions. Exact SHAs and environment caveats are in the
manifest.

## Exact live results

### Click #3678 — parameter named `help`

- BASE: `8c1a0a7abbc1c36f70d1f65f3604acc46c5ce6ab`
- HEAD: `a28d839d3e68bb81b77bcd862fb29e35dcf894f9`
- Claim: selected `claim-help-option-storage-name`.
- Initial candidate: `INVALID_MODEL_OUTPUT`; structured response failed schema validation.
- Repair: used; also `INVALID_MODEL_OUTPUT`.
- Execution: none, because neither attempt produced an artifact.
- Final mechanical status: `INVALID_TEST`.
- Final outcome: `INSUFFICIENT_EVIDENCE`.
- Known usage: 15,858 tokens; 20.286 seconds of completed model calls; 27.430 seconds case wall time.

No generated test source is available for either attempt because both responses failed before a
`CandidateTestProposal` existed. PatchProof retained each attempt's sequence, origin, usage,
validation issue, and raw-response SHA-256, but not the malformed response body. That is an audit
limitation; the attempts were not omitted.

### attrs #1513 — nested `validators.disabled()`

- BASE: `ab7f8b2f4c0f747d5b1575247ce35e5adaad182e`
- HEAD: `91f3695fe70a6d58c95660addd5ef8d46bfebd61`
- Claim: selected `claim-disabled-validator-nesting-state-restoration`.
- Initial candidate: valid, artifact
  `d6883bf168fac33c2bd5bd6dda40ebb10c61543d3c756f59ce14724fbbb7d66b`.
- BASE: `ASSERTION_FAILED` after the inner context incorrectly re-enabled validators.
- HEAD: `PASSED`.
- Initial mechanical result: `DISCRIMINATING`,
  `BASE_ASSERTION_FAILED_HEAD_PASSED`.
- Repair: not used.
- Semantic relation: `RELATED` at 0.98 confidence.
- Final outcome: `CLAIM_SUPPORTED_FOR_SCENARIO`.
- Known usage: 5,529 tokens; 16.812 seconds of model calls; 27.569 seconds case wall time.

Gemini generated:

```python
import attr


def test_disabled_validators_nesting():
    assert attr.validators.get_run_validators() is True
    with attr.validators.disabled():
        assert attr.validators.get_run_validators() is False
        with attr.validators.disabled():
            assert attr.validators.get_run_validators() is False
        assert attr.validators.get_run_validators() is False
    assert attr.validators.get_run_validators() is True
```

This is independently equivalent in behavior to, but not copied from, the hidden oracle.

### marshmallow #2903 — nested partial plus `data_key`

- BASE: `65374df0c31cdc45acc4435741779298201306a2`
- HEAD: `7f8ac62da6150191f465ed44ae00043a4dcb611c`
- Claim: no result. The provider returned `429 RESOURCE_EXHAUSTED` for the free-tier
  `gemini-3.6-flash` request quota (5 requests/minute).
- Candidate, BASE/HEAD execution, repair, and semantic assessment: not reached.
- Terminal status: `CLAIM_INVOCATION_ERROR`.
- Case wall time: 7.339 seconds. Token usage for the failed call was not returned by the adapter.

This is an environmental/provider limitation. It is not a Gemini reasoning failure and not a
PatchProof abstention. The case was not rerun after the quota window reset.

### Astroid #3075 — tokenizer error during position extraction

- BASE: `3258d5ff67d39b0a04383066f7a135aaf7edc422`
- HEAD: `29a23547ed755dcd53c086097449999608511002`
- Claim: selected `claim-tree-rebuilder-get-position-info-token-error-returns-none`.
- Initial candidate: `INVALID_MODEL_OUTPUT`; structured response failed schema validation.
- Repair: used; also `INVALID_MODEL_OUTPUT`.
- Execution: none.
- Final mechanical status: `INVALID_TEST`.
- Final outcome: `INSUFFICIENT_EVIDENCE`.
- Known usage: 10,860 tokens; 21.963 seconds of model calls; 47.927 seconds case wall time.

As with Click, the invalid attempts retain hashes, usage, lineage, and failure codes but no parsed
test source.

### Local synthetic — deepest owning workspace

- BASE: `da3210f15fdf50fafa7005b988d0baddc237f443`
- HEAD: `0ad93cdffbdfb51d53bd99d28c4092138932179d`
- Claim: selected `claim-resolve-owner-deepest-workspace`.
- Initial candidate: statically valid, but its filename was
  `patchproof_generated_tests/test_workspace_registry.com.py`. Pytest interpreted the extra dot as
  a module boundary, so BASE and HEAD both produced `COLLECTION_ERROR`; mechanical status was
  `ENVIRONMENTAL`.
- Repair: used. It changed the path to
  `patchproof_generated_tests/test_workspace_registry.py`; source bytes were otherwise identical.
- Repaired BASE: `ASSERTION_FAILED` because the shallow workspace was returned.
- Repaired HEAD: `PASSED`.
- Final mechanical result: `DISCRIMINATING`,
  `BASE_ASSERTION_FAILED_HEAD_PASSED`.
- Semantic relation: `RELATED` at 0.98 confidence.
- Final outcome: `CLAIM_SUPPORTED_FOR_SCENARIO`.
- Known usage: 5,915 tokens; 35.020 seconds of model calls; 44.631 seconds case wall time.

Gemini generated this source for both attempts:

```python
from dataclasses import dataclass
from workspace_registry import resolve_owner


@dataclass
class DummyWorkspace:
    root: str


class DummyRegistry:
    def __init__(self, candidates):
        self._candidates = candidates

    def candidates_for(self, candidate_path: str):
        return self._candidates


def test_resolve_owner_deepest_workspace():
    w_shallow = DummyWorkspace(root="/a/b")
    w_deep = DummyWorkspace(root="/a/b/c/d")
    registry = DummyRegistry([w_shallow, w_deep])
    result = resolve_owner(registry, "/a/b/c/d/file.py")
    assert result is w_deep
```

The candidate uses stand-in objects rather than the production `Workspace` and `ProjectRegistry`.
It still exercises the observable resolver contract because `resolve_owner` consumes the registry
protocol and workspace `root` values. The generated setup is less integrated than the hidden
oracle, which uses all production classes, so the evidence is scoped to the resolver behavior.

## Aggregate model usage

Completed model results reported 38,162 total tokens: 34,114 prompt tokens and 3,728 output tokens.
They accounted for 94.082 seconds of model latency. Thirteen completed logical results each reported
one provider attempt. The failed Marshmallow claim call did not expose token or provider-attempt
accounting, so 38,162 is a lower bound for the declared run rather than a full billing total.

The detailed per-attempt usage, latency, source, artifact hash, BASE/HEAD result, and mechanical
classification are in [`raw.json`](../benchmarks/hard_mode/results/raw.json).

## Failure attribution

### Gemini/model-output failures

- Click: two malformed candidate structured outputs.
- Astroid: two malformed candidate structured outputs.
- Synthetic: the first generated filename contained an extra dot and could not be collected; the
  permitted repair corrected it.

The selected Click claim was narrower than the PR's user-visible parsing behavior: it targeted the
new automatic help option storage name. It was grounded and testable but implementation-oriented.
The Astroid claim targeted a private method rather than the higher-level builder interface. Neither
claim reached execution because candidate output was malformed.

### PatchProof implementation defect

The candidate path validator accepted `test_workspace_registry.com.py` because it checked only the
`test_` prefix and final `.py` suffix. Pytest treated the embedded dot as an import boundary. After
the sealed evaluation, the validator was tightened to accept only `test_[A-Za-z0-9_]+.py`, with a
regression test. The benchmark was not rerun and raw results were not changed.

### Environmental/provider failure

The Marshmallow case hit the Gemini free-tier request-per-minute quota. The declared protocol did
not include cross-case pacing, and PatchProof's bounded retry policy does not permit waiting and
restarting a failed case. The failure remains part of the result.

### Audit limitation

Malformed candidate response bodies are not retained by the current production evidence model;
only their SHA-256, usage, lineage, and validation issue are retained. This prevents post-hoc
inspection of exactly why Click and Astroid failed schema parsing. Changing that evidence schema
would be broader than the one proven execution defect, so it was documented rather than changed.

## What this demonstrates

Within this small run, PatchProof derived and mechanically proved:

- a nested state-restoration regression involving two context-manager levels; and
- a depth/precedence resolution regression involving multiple candidate objects, including recovery
  from an initially non-collectable artifact through the single permitted repair.

Both supported artifacts failed by assertion on the historical/synthetic BASE, passed on HEAD,
matched the hidden oracle direction, and were judged related to the selected claim. This is
materially harder than the title-whitespace demo because the assertion required multi-step setup,
intermediate state or interacting objects, and counterfactual execution rather than one scalar
normalization call.

## What this does not demonstrate

This evaluation does not show that PatchProof generally handles parser/AST changes, nested schema
deserialization, or CLI parser collisions. The only AST and CLI cases failed before execution, and
the schema case was blocked before claim selection. It does not establish repository-wide
correctness, PR safety, general accuracy, a production false-support rate, or robustness under free
tier quotas. Five deliberately difficult cases are too few for those claims.

It also does not prove the synthetic candidate exercised every production component: Gemini used a
minimal compatible registry/workspace protocol. The hidden oracle provides the stronger integrated
fixture, while the generated evidence supports only the selected resolver scenario.

## Reproduction

The hidden-oracle preflight is reproducible with:

```powershell
uv run python -m patchproof.hard_mode verify-oracles
```

Use a new empty runtime directory because an existing gate is never overwritten. The historical
repositories require network access only for Git preparation; test execution itself is offline.

The checked-in result was produced once with:

```powershell
$env:GOOGLE_API_KEY=(Get-Content -Raw "C:\secure\patchproof-gemini-key.txt").Trim()
uv run python -m patchproof.hard_mode run-live
uv run python -m patchproof.hard_mode summarize
```

Do not run `run-live` expecting it to replace the checked-in result: the local exactly-once journal
blocks that declared run by design. A future evaluation must use a new manifest declaration and
must report all of its cases.
