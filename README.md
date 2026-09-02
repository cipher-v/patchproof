# PatchProof

> Proof for the claim, not a verdict on the PR.

PatchProof is an agentic GitHub PR verification system for explicitly allowlisted public
Python/pytest repositories. Given a pull-request URL, it independently resolves immutable BASE and
HEAD revisions, investigates one falsifiable behavioral claim, generates a bounded pytest
candidate, and executes the exact same candidate bytes on both revisions. It reports support only
when deterministic differential evidence and a final semantic relevance check agree.

PatchProof is deliberately claim-scoped. It does not certify an entire pull request, replace code
review, or claim to safely execute arbitrary hostile repositories.

## Evidence contract

The central invariant is intentionally stricter than “the test passes on HEAD”:

```text
CLAIM_SUPPORTED_FOR_SCENARIO
    only if
BASE = ASSERTION_FAILED
and HEAD = PASSED
and semantic assessment = RELATED
```

A HEAD pass alone is never supporting evidence. `TEST_ERROR + PASSED`, collection failures,
process failures, and other non-assertion differentials remain non-supporting even if a model finds
the result plausible. Semantic assessment runs after any mechanically `DISCRIMINATING` BASE/HEAD
pattern. Claim support is possible only for `BASE=ASSERTION_FAILED` plus `HEAD=PASSED`; the reverse
`BASE=PASSED` plus `HEAD=ASSERTION_FAILED` may only indicate `POTENTIAL_REGRESSION`. Semantic
assessment cannot upgrade weak evidence or reverse the mechanical direction.

Every run terminates conservatively as one of:

- `CLAIM_SUPPORTED_FOR_SCENARIO`
- `INSUFFICIENT_EVIDENCE`
- `POTENTIAL_REGRESSION`

## How it works

```text
GitHub PR URL
  -> allowlist check
  -> server-side GitHub resolution of immutable BASE and HEAD SHAs
  -> deterministic repository context and bounded investigation tools
  -> Google ADK + Gemini 3.6 Flash selects one falsifiable behavioral claim
  -> bounded pytest candidate generation
  -> deterministic syntax, import, path, collection, and safety validation
  -> exact candidate bytes executed on BASE and HEAD
  -> deterministic mechanical classification
  -> up to two evidence-driven repairs when needed
  -> semantic RELATED gate only after mechanical discrimination
  -> hash-verified, claim-scoped evidence result
```

The model cannot choose revisions, installation commands, evidence rules, or the mechanical
result. Candidate generation is bounded to one initial attempt plus at most two repairs, and each
repair receives evidence from the immediately preceding attempt. Installation comes from an
identical repository-owned `.patchproof.yaml` contract or from fixed deterministic templates
selected independently for BASE and HEAD. Unsupported or asymmetric environments fail closed.

This is not a shallow retrieval wrapper: Git identities, context bounds, environment plans,
candidate validation, artifact bytes, process results, differential classification, retry limits,
and evidence hashes are enforced outside the model.

## Sealed fresh evaluation v1

PatchProof was frozen and evaluated once on 16 public historical PRs that were absent from the
PatchProof development corpus before the evaluation freeze: eight behavioral-change positives and
eight negative controls.

| Measure | Result |
| --- | ---: |
| Behavioral-change positives supported | 8/8 |
| Negative controls correctly abstained | 7/8 |
| Overall outcome agreement | 15/16 (93.75%) |
| Support precision | 8/9 (≈88.89%) |
| Positive recall | 100% |
| F1 | ≈94.12% |
| Environment failures | 0 |
| Provider/model invocation failures | 0 |
| Harness failures | 0 |
| Invalid claim outputs | 0 |
| Potential regressions | 0 |
| Candidate attempts | 11 |
| Cases requiring repair | 2 |
| Claims selected / claim abstentions | 9 / 7 |

“Fresh” means only that these cases were absent from the PatchProof development corpus before the
evaluation freeze. The PRs are public and may have appeared in Gemini training data. These results
must not be described as unseen-data accuracy, generalization accuracy, universal correctness, or
a guarantee for arbitrary future PRs.

The one false support was
[`requests` #6951](https://github.com/psf/requests/pull/6951). Its evidence was mechanically valid,
but the benchmark classified the changed Sphinx `docs/conf.py` behavior as non-product behavior.
This is a known behavioral-scope/applicability limitation: mechanically observable behavior is not
always behavior the product should claim to verify.

`fresh-eval-v1` is permanently closed. It must not be rerun after product changes, and its sealed
result is not used as a tuning loop. The protocol, integrity record, results, and limitations are
documented in
[`docs/evaluations/fresh_eval_v1.md`](docs/evaluations/fresh_eval_v1.md) and
[`benchmarks/fresh_eval_v1/README.md`](benchmarks/fresh_eval_v1/README.md).

## Production evidence

The current cloud product is live at the
[PatchProof evidence console](https://patchproof-control-q26kc4fdba-el.a.run.app/dashboard).

A production canary analyzed
[`pallets/click` #3805](https://github.com/pallets/click/pull/3805), “Fix `copy`, `deepcopy` and
`pickle` of `Sentinel` members.” PatchProof independently resolved:

[Open the Click #3805 canary run in the live evidence console.](https://patchproof-control-q26kc4fdba-el.a.run.app/dashboard?run=35b10b2c-d5de-4461-8c52-dfe8c7d70bda)

```text
BASE  3cbcf9b11546f4cf10b36d3e2e531733ba6fe001
HEAD  f58ca3e81424a35626c8a475eb59ab95589008ce
```

It selected the claim that pickling `Sentinel.UNSET` succeeds on HEAD through custom
`__reduce_ex__`, while BASE raises `TypeError` because `object()` cannot be pickled.

| Attempt | BASE | HEAD | Mechanical result | Outcome |
| --- | --- | --- | --- | --- |
| Initial | `TEST_ERROR` | `PASSED` | `UNCAUGHT_EXCEPTION_ON_ONE_REVISION` | Rejected as non-supporting |
| Repair | `ASSERTION_FAILED` | `PASSED` | `DISCRIMINATING` | Semantic `RELATED`; claim supported |

The initial HEAD pass was not accepted. Support became possible only after the repaired candidate
produced the admissible mechanical pattern. The final evidence SHA-256 is:

```text
eb2421a6ffc959458f85de45c57434e43ce49ece31caffcbc4894ceaf424161c
```

This is production/demo evidence, not part of the sealed 16-PR evaluation.

## Product boundary and limitations

Supported scope:

- explicitly allowlisted public GitHub repositories;
- any PR number within those repositories;
- Python 3.12;
- pytest;
- deterministic unit tests or small integration scenarios;
- repository environments that produce one supported, equivalent BASE/HEAD execution plan.

The repository allowlist is a security and cost trust boundary. Supporting arbitrary PR numbers
inside that allowlist does **not** mean PatchProof supports arbitrary hostile repositories.
Hostile-code isolation is not implemented.

PatchProof also does not:

- prove whole-PR correctness;
- establish that no regression exists outside the selected scenario;
- guarantee that a valid observable is within the intended product scope;
- replace human code review, CI, or repository-specific testing;
- let model confidence substitute for executable evidence.

Normal GitHub App webhook execution requires a committed `.patchproof.yaml` that is valid and
identical on BASE and HEAD. URL-submitted cloud analysis can instead probe committed packaging
metadata on both revisions and proceed only when fixed validated templates produce equivalent
plans. Missing, unsupported, mismatched, or failed setup terminates conservatively.

## Cloud architecture

The deployed system uses:

- a public Google Cloud Run control service;
- an IAM-private Cloud Run executor;
- OIDC-authenticated Cloud Tasks;
- Firestore as the durable source of truth;
- Secret Manager for operator-controlled credentials;
- Vertex AI, Google ADK, and Gemini 3.6 Flash;
- Python 3.12 containers in `asia-south1`;
- the clean Firestore namespace `patchproof-final-v1`.

The control service resolves PR metadata, persists the immutable identity, retrieves deterministic
context, and coordinates model work. Candidate execution happens in the private executor with a
credential-minimized environment, bounded output capture, process-tree timeouts, and identical
BASE/HEAD requests. The evidence console reads durable Firestore records, verifies evidence hashes,
and exposes sanitized claim, candidate-lineage, execution, abstention, and final-result data.

Production cloud analysis does not depend on benchmark manifests, hidden oracles, `HardModeCase`,
or expected BASE/HEAD labels. See
[`docs/23_CLOUD_ANALYZE_INTEGRATION.md`](docs/23_CLOUD_ANALYZE_INTEGRATION.md) for the boundary and
deployment behavior.

## Analyze an allowed PR

With the deployed control URL configured, the CLI creates or reuses one durable cloud run and
polls the same sanitized result shown by the evidence console:

```powershell
$env:PATCHPROOF_CONTROL_URL="https://patchproof-control-q26kc4fdba-el.a.run.app"
uv run patchproof analyze "https://github.com/pallets/click/pull/3805"
```

The equivalent API request is:

```http
POST /api/analyze
Content-Type: application/json

{"pr_url":"https://github.com/pallets/click/pull/3805"}
```

The server, not the client, resolves and stores BASE and HEAD. Malformed URLs, repositories outside
the deployment allowlist, invalid PR metadata, transient GitHub failures, and unsupported execution
plans remain distinct fail-closed outcomes.

Without `PATCHPROOF_CONTROL_URL`, `patchproof analyze` retains the manifest-backed local historical
demonstration. `--cloud` and `--local` are explicit overrides.

To run the local evidence-console preview with the checked sanitized fixture:

```shell
uv run python -m patchproof.dashboard_preview
```

Then open `http://127.0.0.1:8092/dashboard`.

## Local development

Install Python 3.12 and [uv](https://docs.astral.sh/uv/), then run:

```shell
uv sync --frozen --all-groups
uv run python -m pytest -q
uv run ruff check .
uv run ruff format --check .
```

If uv's global cache is unavailable, point `UV_CACHE_DIR` at a writable local directory.

To start the local webhook control plane, provide a secret, repository allowlist, and database path:

```shell
uv run uvicorn patchproof.control_plane:create_app --factory
```

The factory reads `PATCHPROOF_WEBHOOK_SECRET`, comma-separated
`PATCHPROOF_ALLOWED_REPOSITORIES`, and optional `PATCHPROOF_DATABASE_PATH`. Never commit webhook
secrets or the local database.

PatchProof selects either the Gemini Developer API or Vertex AI explicitly while retaining the same
ADK agents and structured schemas. Vertex uses Application Default Credentials locally and the
attached runtime service account on Cloud Run; no service-account key file is required. Provider
setup and safe preflight commands are documented in
[`docs/VERTEX_AI_PROVIDER.md`](docs/VERTEX_AI_PROVIDER.md).

## Historical development evidence

The following runs remain useful engineering evidence, but they predate the sealed evaluation and
current arbitrary-PR product path.

### PatchProof Bench

The four-case developer regression bench replays genuine historical PRs from `more-itertools` and
`humanize`. All four reproduced BASE assertion failure / HEAD pass, and controlled weak no-op
candidates were rejected as non-discriminating. In that small policy comparison, HEAD-only
acceptance produced four false supports while the BASE/HEAD rule produced none.

```shell
uv run python -m patchproof.benchmark verify
uv run python -m patchproof.benchmark run
uv run python -m patchproof.benchmark summarize
```

See [`benchmarks/results/summary.md`](benchmarks/results/summary.md) and
[`benchmarks/results/raw.json`](benchmarks/results/raw.json).

### Earlier live workflow

An explicitly authorized Gemini workflow on `more-itertools` PR 1223 selected a grounded claim,
repaired one environmental candidate, and produced identical-artifact BASE assertion failure /
HEAD pass with semantic `RELATED`. It used four provider attempts and 12,225 total tokens. This was
one-case workflow evidence, not a blind candidate-generation benchmark.

### Hard-mode diagnostic

The separate blind hard-mode diagnostic withheld changed upstream Python tests and used independent
hidden oracles. In one declared, non-repeated Gemini 3.6 Flash run over four historical PRs plus one
difficult local fixture, two cases produced claim-scoped support, two exhausted both historical
candidate slots on malformed structured output, and one was blocked by the Gemini free-tier quota.
No generated support disagreed with the hidden-oracle differential direction. These are diagnostic
counts, not an accuracy claim. See
[`docs/10_HARD_MODE_EVALUATION.md`](docs/10_HARD_MODE_EVALUATION.md).

The checked exactly-once hard-mode result is under
[`benchmarks/hard_mode/results/`](benchmarks/hard_mode/results/). Its journal deliberately refuses
to replace that historical run.

## Repository guide

```text
src/patchproof/            Product and evaluation Python package
tests/                     Automated unit, integration, invariant, and offline harness tests
docs/                      Architecture, deployment, evaluation, and operator documentation
benchmarks/                Historical and sealed evaluation definitions and artifacts
```

Start with:

- [`docs/00_PROJECT_SPEC.md`](docs/00_PROJECT_SPEC.md) — scope and claim contract;
- [`docs/03_PHASE_AGENT_CLAIMS.md`](docs/03_PHASE_AGENT_CLAIMS.md) — deterministic context and claim selection;
- [`docs/04_PHASE_TEST_GENERATION.md`](docs/04_PHASE_TEST_GENERATION.md) — candidate generation, validation, and repair;
- [`docs/05_PHASE_EVIDENCE_WORKFLOW.md`](docs/05_PHASE_EVIDENCE_WORKFLOW.md) — orchestration and durable evidence;
- [`docs/06_PHASE_RELIABILITY_SECURITY.md`](docs/06_PHASE_RELIABILITY_SECURITY.md) — reliability and security boundaries;
- [`docs/08_PHASE_CLOUD_DEPLOYMENT.md`](docs/08_PHASE_CLOUD_DEPLOYMENT.md) — deployed services and IAM boundaries;
- [`docs/09_PHASE_DASHBOARD_DEMO.md`](docs/09_PHASE_DASHBOARD_DEMO.md) — evidence-console behavior;
- [`docs/23_CLOUD_ANALYZE_INTEGRATION.md`](docs/23_CLOUD_ANALYZE_INTEGRATION.md) — arbitrary-PR cloud integration.
