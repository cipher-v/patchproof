# PatchProof fresh evaluation v1

This directory contains a sealed, genuinely fresh evaluation set constructed against
PatchProof Phase 2 implementation
`851b8342b3aac6a0c1664c519b5c1827a1fe6079`. Construction used no Gemini or other
language-model calls. The evaluation itself has not been run.

## Contents

- `selection_protocol.md` freezes the inclusion, exclusion, diversity, compatibility,
  ambiguity, leakage, and discovery rules that were written before cases were accepted.
- `candidate_ledger.json` records accepted and rejected candidates, including
  environment-driven replacements.
- `manifest.json` contains 8 positive and 8 negative-control cases from 12 repositories,
  immutable PR provenance, exact changed-test exclusions, symmetric install plans, and
  construction-readiness results.
- `oracles/` contains one independent hidden oracle for each positive. Every oracle was
  verified through the normal injected pytest path as `BASE=ASSERTION_FAILED` and
  `HEAD=PASSED`.
- `evaluation.py` validates integrity, constructs the label-blind case boundary, scores
  results only after inference, and performs zero-model construction verification.
- `integrity.json` seals the protocol, ledger, manifest, and oracle bytes.

Negative controls do not have oracles. Their `label`, `expected_interpretation`,
`oracle`, and `construction_readiness` fields are sealed evaluator metadata. The
`inference_case()` boundary omits every one of those fields. Expected abstention is
applied only by `score_result()` after PatchProof has returned a result.

## Leakage boundary

Every changed upstream Python test or static typing test is listed in `excluded_paths`.
The same exclusions are used for deterministic context retrieval and both revision
indexes. Construction also verifies that excluded paths do not appear in the Phase-2
starting context. Oracle source is never part of the inference case.

## Construction verification

From the repository root, with a short cache and workspace path on Windows:

```powershell
$cache = Join-Path $env:TEMP "patchproof-fresh-eval-repositories"
$work = Join-Path $env:TEMP "fe"
uv run python -m benchmarks.fresh_eval_v1.evaluation `
  --cache-root $cache `
  --workspace-root $work
```

The command makes no model calls. It verifies both revision commits, changed-test
exclusions, equivalent deterministic install plans, BASE/HEAD environment readiness
through the candidate-style injected node, Phase-2 index/planner construction, and the
eight positive oracle directions. It exits nonzero if any case is not ready.

## Sealed evaluation policy

Do not use this set for prompt changes, debugging, threshold selection, investigation
budget changes, validator changes, or repeated exploratory runs. Before an authorized
evaluation, confirm the PatchProof production source remains unchanged relative to the
frozen implementation SHA. Run each declared case once, persist raw results, and score
only after inference through the hidden manifest label. Environment or provider-invalid
runs are not negative abstentions and must be reported separately.

The set intentionally includes two distinct cases each from Boltons, Tablib, Typeguard,
and Requests; no repository contributes more than two. Positives cover serialization,
range validation, URL normalization, tabular collections, text inflection, runtime type
validation, mutation safety, and copy semantics. Controls cover typing, comments,
formatting/lint cleanup, tests/private cleanup, lint configuration, and documentation.

## Known limitations

- This is a small 16-case evaluation, not a population estimate for all Python PRs.
- Repository selection favors deterministic pure-Python projects that install on Python
  3.12 through PatchProof's existing validated templates.
- Public PR prose can describe the intended change; this is product input, not a hidden
  oracle. Changed upstream Python tests remain withheld.
- Construction proves readiness and oracle direction, not future Gemini quality. No
  sealed evaluation result exists yet.
