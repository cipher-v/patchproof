# Unseen Historical PR Holdout Construction

## Method

PatchProof production behavior was frozen at
`baf333afc160cd75a90cea1e0568120a9889fb7e` before holdout selection. The final
development-evaluation commit was
`3b724d33b42a533002e13cc670a43632b4dcd35d`. The five development cases V1-V5
were neither modified nor rerun, and no V6 dataset was created.

The selection rules in `benchmarks/holdout/HOLDOUT_PROTOCOL.md` and
`benchmarks/holdout/selection_protocol.json` were written and staged before any candidate
search. Candidate choice was based on merger provenance, deterministic Python 3.12
reproduction, repository and behavioral diversity, bounded environment complexity, and
oracle stability. Perceived model difficulty was not used.

For every selected PR, BASE and HEAD are the frozen GitHub PR base and head commits.
`refs/pull/<number>/head` resolves to the declared HEAD, and `BASE..HEAD` is the reviewed PR
patch. GitHub's public API confirmed every selected PR is merged and supplied timestamps and
changed paths.

Each regression check was written independently from the public PR description, production
behavior, and public interface. Upstream regression tests were not copied. The same oracle
file, addressed outside the evaluated checkout, was executed against BASE and HEAD. Its
SHA-256 was measured once and checked against the manifest.

## Candidate pool

Seventeen serious candidates across thirteen public repositories were investigated. Ten
were selected. The complete ledger, including exact revisions and changed paths, is in
`benchmarks/holdout/candidate_ledger.json`.

Seven candidates were not selected:

| Repository / PR | Outcome | Reason |
|---|---|---|
| encode/httpx #3364 | Rejected | The PR describes a refactor, not a demonstrated behavioral bug fix. |
| python-hyper/h11 #183 | Rejected | The sole change is a documentation link. |
| urllib3/urllib3 #4952 | Rejected | Source import needs generated metadata and an independent public reproduction required extensive connection mocking; a lower-complexity diverse case ranked higher. |
| Textualize/rich #3935 | Rejected | Deterministic public rendering probes did not reliably distinguish the revisions. |
| python-attrs/cattrs #638 | Rejected | Independent public `Converter.structure` scenarios did not reproduce the defect reliably. |
| pypa/packaging #1384 | Qualified, not selected | Redundant with the selected packaging parser case under the one-case-per-repository preference. |
| more-itertools/more-itertools #1193 | Qualified, not selected | Redundant with the selected sequence case under the one-case-per-repository preference. |

The two qualified alternatives were retained in the ledger to show that final selection was a
deterministic diversity decision rather than a hidden eligibility judgment.

## Final dataset

| Case | Repository | PR | Category | BASE result | HEAD result |
|---|---|---:|---|---|---|
| `jsonschema-1208-enum-equality` | python-jsonschema/jsonschema | [#1208](https://github.com/python-jsonschema/jsonschema/pull/1208) | Validation and recursive equality | `ASSERTION_FAILED` | `PASSED` |
| `dateutil-751-midnight-rollover` | dateutil/dateutil | [#751](https://github.com/dateutil/dateutil/pull/751) | Date-time parsing | `ASSERTION_FAILED` | `PASSED` |
| `more-itertools-1128-negative-slice` | more-itertools/more-itertools | [#1128](https://github.com/more-itertools/more-itertools/pull/1128) | Sequence transformation | `ASSERTION_FAILED` | `PASSED` |
| `packaging-1345-true-end` | pypa/packaging | [#1345](https://github.com/pypa/packaging/pull/1345) | Requirement parsing | `ASSERTION_FAILED` | `PASSED` |
| `starlette-3317-authorityless-url` | Kludex/starlette | [#3317](https://github.com/Kludex/starlette/pull/3317) | URL construction and error handling | `ASSERTION_FAILED` | `PASSED` |
| `rich-3938-soft-wrap-background` | Textualize/rich | [#3938](https://github.com/Textualize/rich/pull/3938) | Terminal text rendering | `ASSERTION_FAILED` | `PASSED` |
| `jinja-2029-missing-singleton` | pallets/jinja | [#2029](https://github.com/pallets/jinja/pull/2029) | Serialization and singleton identity | `ASSERTION_FAILED` | `PASSED` |
| `platformdirs-523-separator-only-xdg` | tox-dev/platformdirs | [#523](https://github.com/tox-dev/platformdirs/pull/523) | Filesystem paths and configuration | `ASSERTION_FAILED` | `PASSED` |
| `anyio-1200-empty-stem` | agronholm/anyio | [#1200](https://github.com/agronholm/anyio/pull/1200) | Path manipulation and validation | `ASSERTION_FAILED` | `PASSED` |
| `cattrs-696-converted-default` | python-attrs/cattrs | [#696](https://github.com/python-attrs/cattrs/pull/696) | Serialization and converted defaults | `ASSERTION_FAILED` | `PASSED` |

## Diversity

The final dataset contains ten repositories, one case per repository. Its behavioral surface
includes recursive validation, date-time parsing, negative-step sequence slicing, strict
requirement parsing, URL reconstruction, ANSI terminal rendering, singleton pickle/copy
semantics, environment-derived filesystem paths, pathlib-compatible validation, and
converter-aware serialization defaults.

The environment set is intentionally bounded but varied:

- All ten cases import a checkout root or `src` directory directly.
- The dateutil oracle isolates `isoparser` and supplies the minimal `six`/timezone namespace
  required by this timezone-free case.
- The Jinja oracle isolates `jinja2.utils` and supplies a minimal deterministic MarkupSafe
  namespace; the sentinel behavior itself does not use MarkupSafe.
- The platformdirs oracle supplies deterministic build-generated version metadata and tests
  the Unix backend directly.
- All gates use Python 3.12.6, local deterministic pytest, no credentials, no live services,
  and no network during oracle execution.

## Changed-test withholding

Every Python test path changed by a selected PR is recorded in that case's `excluded_paths`:

- dateutil #751: `dateutil/test/test_isoparser.py`
- more-itertools #1128: `tests/test_more.py`
- packaging #1345: `tests/test_markers.py`, `tests/test_requirements.py`
- Starlette #3317: `tests/test_datastructures.py`
- Rich #3938: `tests/test_segment.py`, `tests/test_text.py`
- Jinja #2029: `tests/test_utils.py`
- platformdirs #523: `tests/conftest.py`, `tests/test_macos.py`, `tests/test_unix.py`,
  `tests/test_windows.py`
- AnyIO #1200: `tests/test_fileio.py`
- cattrs #696: `tests/test_converter.py`
- jsonschema #1208 changed no Python test path.

Existing `DeterministicContextRetriever` behavior was inspected: exact excluded paths are
removed before changed-file diff processing, changed-symbol and snippet derivation, the
repository Python-path scan, test/reference retrieval, and bounded signature extraction.
The targeted exclusion tests passed (`2 passed`):

- `test_exact_excluded_path_is_absent_from_every_prompt_bound_context_section`
- `test_signature_context_never_reads_excluded_or_other_test_files`

## Deterministic gate

`benchmarks/holdout/oracle_gate.json` records all ten exact BASE/HEAD pairs, oracle hashes,
pytest exit codes, classified results, environment modes, and identical-byte confirmations.
Every BASE is `ASSERTION_FAILED`; every HEAD is `PASSED`; and the identical oracle bytes were
used for each pair.

## Evaluation integrity

PatchProof's claim selection, candidate generation, repair, and semantic assessment agents
were not invoked during discovery, selection, oracle writing, or validation. No Gemini or
Vertex model inference occurred, and no model request was attempted. The implementation,
prompts, mechanical classifier, retry policy, and pacing policy were already frozen.

The future run is predeclared as `gemini-3.6-flash` through `VERTEX_AI` in `global`, with one
claim-selection call, one initial candidate, at most one repair, and semantic assessment only
after a mechanically distinguishing result. At most 40 logical model calls and 80 provider
attempts are permitted across ten cases. Future results will be reported, not used to tune the
frozen implementation.

No file under `src/patchproof/` was changed. The construction phase contains only holdout
dataset/evaluation-preparation artifacts and this report. One orchestration blocker must be
resolved before the future evaluation: the frozen `HardModeManifest` validator requires at
least one `LOCAL_SYNTHETIC` case, but this holdout is intentionally exactly ten real historical
PRs. The future run must use a holdout-specific orchestration path that accepts this manifest
without changing frozen prompts, context retrieval, classification, retry, pacing, or semantic
behavior. It also requires the declared Vertex credentials and call budget, and must not start
until the sealed commit is checked out cleanly.
