# Unseen historical holdout results

## Result

PatchProof completed its single authorized blind evaluation over ten sealed historical Python bug
fixes. It established claim support for 2 scenarios and returned insufficient evidence for 8. The
2/10 figure is a descriptive scenario-support proportion only. It is not conventional predictive
accuracy, whole-pull-request correctness, or an estimate of production prevalence.

The evaluated implementation was frozen at
`baf333afc160cd75a90cea1e0568120a9889fb7e`. The holdout was constructed and sealed at
`113fc1af287b42447a7cde6b0a91241b7363c52c`, and the approved benchmark-only orchestration was
frozen at `61afb7cedba1642a0b33689d1146be8731dfa131`.

## Exactly-once execution record

- Run ID: `unseen-holdout-v1-gemini-3.6-flash-vertex-2026-08-29`
- Provider/model: Vertex AI / `gemini-3.6-flash`
- Project/location: `patchproof-506606` / `global`
- Manifest SHA-256: `2192168c32389843c259c4d1dcb945533a49e843cbf6e5a4e13df45449d98d30`
- Journal SHA-256: `dde659ccccad53c57196056c5197aec47f81b81231adc2af28da7695c1b8c438`
- Raw result SHA-256: `c70076f2c5cbc50780f9b99b0563dc05aaef6c51932ba74fb222cdb2e85486b8`
- Context-gate SHA-256: `e5e6e5d87337a1ce547172b270a11ec7ed2a5aa414f65ed433cbdca101c1e8be`
- Journal run-start/run-complete events: 1 / 1
- Journal case-start/case-complete events: 10 / 10
- Case reruns: 0

The `oracle_gate_sha256` field inherited by `raw.json` names the no-oracle live context gate
created by the holdout adapter. It is not the sealed developer oracle gate used to prove the
historical BASE/HEAD regression checks during construction. The exact live context gate is retained
beside the results for auditability.

## Aggregate accounting

| Measure | Result |
|---|---:|
| Total/historical cases | 10 / 10 |
| Claims selected | 10 |
| Claim abstentions | 0 |
| Invalid claim outputs | 0 |
| Provider-terminal failures | 0 |
| Candidate attempts | 18 |
| Validated candidates | 17 |
| Valid initial candidates | 9 |
| Invalid candidate structured outputs | 0 |
| Initial candidates executed | 9 |
| Repairs attempted/executed | 8 / 8 |
| Repairs rejected as behavioral no-op | 0 |
| Repairs producing discrimination | 0 |
| Discriminating cases | 2 |
| Supported scenarios | 2 |
| Insufficient-evidence cases | 8 |
| Environmental evaluations | 13 |
| Non-discriminating evaluations | 2 |
| Invalid-test terminal outcomes | 0 |
| Potential regressions | 0 |
| Incorrect supports | 0 |
| Logical model results | 30 |
| Provider attempts | 30 |
| Prompt tokens | 121,351 |
| Output tokens | 6,205 |
| Total tokens | 128,767 |
| Total model duration | 144.234 seconds |

The declared worst-case budgets were 40 logical calls and 80 provider calls. Every completed model
result reports one provider attempt; no terminal provider failure occurred.

## Per-case table

| Case | Claim | Initial | Repair | Mechanical | Semantic | Final |
|---|---|---|---|---|---|---|
| jsonschema #1208 | Enum comparison is type-sensitive | BASE/HEAD both passed | Executed; distinct but both passed again | NON_DISCRIMINATING | — | INSUFFICIENT_EVIDENCE |
| dateutil #751 | ISO hour 24 advances the day | Collection error on both | Executed; distinct but same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| more-itertools #1128 | Negative numeric-range slicing follows sequence semantics | BASE assertion failed; HEAD passed | Not used | DISCRIMINATING | RELATED | CLAIM_SUPPORTED_FOR_SCENARIO |
| packaging #1345 | END tokenizer uses true-end `\\Z` | BASE assertion failed; HEAD passed | Not used | DISCRIMINATING | RELATED | CLAIM_SUPPORTED_FOR_SCENARIO |
| Starlette #3317 | Authorityless URL replacement avoids IndexError | BASE test error; HEAD passed | Executed; distinct, same direction | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| Rich #3938 | Segment splitting preserves line-terminator information | BASE test error; HEAD passed | Executed; distinct, same direction | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| Jinja #2029 | Missing singleton can be pickle-round-tripped | Collection error on both | Executed; distinct but same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| platformdirs #523 | Separator-only XDG lists fall back | Collection error on both | Executed; distinct but same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| AnyIO #1200 | Empty stem with suffix raises ValueError | Process error on both | Executed; distinct but same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| cattrs #696 | omit_if_default uses converted defaults | Rejected by deterministic import grounding | Validated and executed; process error on both | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |

## Case notes

### jsonschema #1208

The selected claim targeted type-sensitive enum equality. The initial candidate and a
behaviorally distinct repair both validated and executed, but both passed on BASE and HEAD. No
semantic assessment was permitted because neither candidate discriminated.

### dateutil #751

The selected claim targeted `isoparse` day advancement for hour 24. Both validated candidates
encountered collection errors on both revisions, leaving the result environmental.

### more-itertools #1128

The initial validated candidate exercised negative-step slicing. Its identical assertion failed on
BASE and passed on HEAD. Mechanical evidence was discriminating, semantic assessment was related,
and no repair was needed. The case is supported.

### packaging #1345

The initial validated candidate checked the END tokenizer's true-end expression. Its identical
assertion failed on BASE and passed on HEAD. Mechanical evidence was discriminating, semantic
assessment was related, and no repair was needed. The case is supported.

### Starlette #3317

The claim targeted avoiding `IndexError` while replacing components of an authorityless URL. Both
validated candidates produced a BASE test error and HEAD pass. Under the frozen classifier this is
not comparable assertion evidence, so the case remains environmental and insufficient.

### Rich #3938

The claim targeted line splitting and terminator information. Both validated candidates produced a
BASE test error and HEAD pass. The frozen support requirement correctly refused to convert that
direction into support without a BASE assertion failure.

### Jinja #2029

The claim targeted pickle stability of the missing singleton. Both validated candidates encountered
collection errors on both revisions, so the case remains environmental.

### platformdirs #523

The claim targeted filtering separator-only XDG directory entries. Both validated candidates
encountered collection errors on both revisions, so the case remains environmental.

### AnyIO #1200

The claim targeted `Path.with_stem` validation for an empty stem. Both validated candidates
encountered process errors on both revisions, including after repair.

### cattrs #696

The claim targeted converter-applied defaults under `omit_if_default`. The initial candidate was
rejected because import root `attrs` was absent from deterministic context. The repair validated
and executed, but both revisions encountered process errors.

## False-support audit

Both supported cases satisfy every frozen requirement:

- BASE status was `ASSERTION_FAILED`;
- HEAD status was `PASSED`;
- mechanical status was `DISCRIMINATING`;
- semantic relation was `RELATED`;
- the generated direction matched the sealed oracle direction.

Incorrect supports: **0**. Potential regressions: **0**.

## Category observations

The sequence-transformation and requirement-parsing cases were supported. The validation/equality
case was non-discriminating. The date-time, URL, terminal-rendering, singleton-serialization,
filesystem/environment, path-validation, and converted-default cases were environmental. Because
each behavioral category contains only one holdout case, these are diagnostic breadth observations,
not significance tests or broad repository-level conclusions.

## Comparison with development V5

Development V5 contained five fixed development cases: three were supported and none were
incorrectly supported. This unseen holdout contains ten cases: two were supported and none were
incorrectly supported. The differing descriptive proportions do not prove a causal improvement or
degradation.

The holdout repositories were not used to tune PatchProof. The production implementation was
frozen before holdout selection. Selection and independent oracle validation occurred without
PatchProof semantic agents. The blind evaluation ran once, and these results were not used to
change the evaluated implementation.

## Observed limitations

- One claim produced two validated but non-discriminating candidates.
- Seven cases ended environmental: collection errors affected dateutil, Jinja, and platformdirs;
  BASE test errors with HEAD passes affected Starlette and Rich; process errors affected AnyIO and
  cattrs.
- Eight repairs were attempted and executed, but none produced discriminating evidence.
- One initial candidate was rejected by deterministic import grounding.

No claim-selection miss, provider-terminal failure, structured-output failure, incorrect support,
or potential regression occurred. These limitations are reported without post-holdout fixes,
tuning, case changes, or reruns.

## Post-result project verification

- `uv run pytest -q`: 295 passed, 1 failed, 1 skipped.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv build`: source distribution and wheel built successfully.

The sole pytest failure is
`test_canonical_preflight_is_read_only_and_resolves_the_frozen_contract`. Its first assertion
requires the predeclared results directory not to exist. That was correct before authorization but
is necessarily false after this exactly-once run created immutable results. The test and
orchestration were deliberately left unchanged under the no-post-holdout-fixes rule.
