# PatchProof sealed unseen holdout result

Run ID: `unseen-holdout-v1-gemini-3.6-flash-vertex-2026-08-29`

The exactly-once blind run completed all ten historical cases. PatchProof supported 2 of 10
holdout scenarios and produced insufficient evidence for 8. This is a descriptive holdout scenario
support proportion, not conventional predictive accuracy, whole-PR correctness, or a
representative production estimate.

## Provenance and artifacts

- Frozen implementation: `baf333afc160cd75a90cea1e0568120a9889fb7e`
- Holdout construction: `113fc1af287b42447a7cde6b0a91241b7363c52c`
- Orchestration: `61afb7cedba1642a0b33689d1146be8731dfa131`
- Manifest SHA-256: `2192168c32389843c259c4d1dcb945533a49e843cbf6e5a4e13df45449d98d30`
- Live journal SHA-256: `dde659ccccad53c57196056c5197aec47f81b81231adc2af28da7695c1b8c438`
- Raw result SHA-256: `c70076f2c5cbc50780f9b99b0563dc05aaef6c51932ba74fb222cdb2e85486b8`
- Live context-gate SHA-256: `e5e6e5d87337a1ce547172b270a11ec7ed2a5aa414f65ed433cbdca101c1e8be`

The raw field named `oracle_gate_sha256` refers to the no-oracle live context gate created by the
holdout orchestration. It does not refer to the sealed developer oracle gate used during holdout
construction.

## Protocol and accounting

- Provider/model: Vertex AI / `gemini-3.6-flash`
- Project/location: `patchproof-506606` / `global`
- Declared maximum logical/provider calls: 40 / 80
- Actual completed logical results/provider attempts: 30 / 30
- Prompt/output/total tokens: 121,351 / 6,205 / 128,767
- Total model duration: 144.234 seconds
- Claims selected/abstained/invalid: 10 / 0 / 0
- Candidate attempts/validated: 18 / 17
- Valid initial candidates/initial candidates executed: 9 / 9
- Repairs attempted/executed/no-op rejected: 8 / 8 / 0
- Discriminating/supported/insufficient-evidence cases: 2 / 2 / 8
- Environmental/non-discriminating evaluations: 13 / 2
- Provider-terminal/structured-output failures: 0 / 0
- Potential regressions/incorrect supports: 0 / 0

## Per-case outcomes

| Case | Claim | Initial | Repair | Mechanical | Semantic | Final |
|---|---|---|---|---|---|---|
| jsonschema #1208 | Type-sensitive enum equality | BOTH_PASSED | Used; distinct, again BOTH_PASSED | NON_DISCRIMINATING | — | INSUFFICIENT_EVIDENCE |
| dateutil #751 | Hour 24 advances one day | Collection error on both | Used; distinct, same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| more-itertools #1128 | Negative slicing follows sequence semantics | BASE assertion failed; HEAD passed | Not used | DISCRIMINATING | RELATED | CLAIM_SUPPORTED_FOR_SCENARIO |
| packaging #1345 | END token uses true-end `\\Z` | BASE assertion failed; HEAD passed | Not used | DISCRIMINATING | RELATED | CLAIM_SUPPORTED_FOR_SCENARIO |
| Starlette #3317 | Authorityless URL replacement avoids IndexError | BASE test error; HEAD passed | Used; distinct, same direction | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| Rich #3938 | Line splitting reports terminators | BASE test error; HEAD passed | Used; distinct, same direction | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| Jinja #2029 | Missing singleton is pickle-stable | Collection error on both | Used; distinct, same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| platformdirs #523 | Separator-only XDG entries fall back | Collection error on both | Used; distinct, same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| AnyIO #1200 | Empty stem with suffix raises | Process error on both | Used; distinct, same environment result | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |
| cattrs #696 | Converted defaults are omitted | Initial rejected by import grounding | Used; validated; process error on both | ENVIRONMENTAL | — | INSUFFICIENT_EVIDENCE |

Every supported case passed the false-support audit: BASE assertion failed, HEAD passed,
mechanical status was discriminating, semantic relation was related, and the generated direction
matched the sealed oracle direction. Incorrect supports: zero.

## Verification

- Pytest: 295 passed, 1 failed, 1 skipped.
- Ruff: passed.
- Format check: passed.
- Package build: passed.

The sole pytest failure is the orchestration test's pre-run assertion that
`benchmarks/holdout/results/` must not exist. That assumption is false only because the authorized
immutable results now exist. The test and orchestration were not changed after observing the
holdout.

## Interpretation

The sequence-transformation and requirement-parsing cases were supported. Each other behavioral
category contains only one case and produced insufficient evidence, so category differences are
diagnostic observations rather than statistically generalizable results.

Development V5 supported 3 of 5 fixed development cases with zero incorrect supports; this unseen
holdout supported 2 of 10 with zero incorrect supports. The difference does not establish a causal
improvement or degradation. The holdout repositories were not used for tuning, implementation was
frozen before selection, selection and oracle validation used no PatchProof semantic agents, the
blind run occurred once, and no result was used to change the evaluated implementation.

Observed limitations were one non-discriminating case, seven environmental terminal cases, eight
repairs that did not produce discrimination, and one initial candidate rejected by deterministic
import grounding. The pre-run-only results-directory test also fails after result creation and was
preserved unchanged. There were no claim-selection misses, provider-terminal failures, structured
output failures, potential regressions, or incorrect supports.
