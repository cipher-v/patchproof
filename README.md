# PatchProof

> Proof for the claim, not a verdict on the PR.

PatchProof is an event-driven GitHub agent intended to select one high-confidence behavioral
claim from an allowlisted Python/pytest pull request, construct bounded candidate regression
tests, challenge the identical test artifact against immutable BASE and HEAD revisions, reject
weak evidence, and publish a conservative claim-scoped report.

## Project status

**Phase 9 implementation and live deployment complete; genuine screenshots remain pending.**
PatchProof reads
immutable BASE/HEAD Git objects, derives changed Python symbols with the AST, and ranks bounded
diff, source, test, import, and reference evidence. One logical stateless, tool-free Google ADK
agent using Gemini 3.6 Flash may select one grounded claim and propose one structured pytest
candidate. Deterministic validation enforces the repository's `.patchproof.yaml`, target path,
syntax, collection shape, imports, selected unsafe calls, immutable bytes, and artifact hash. It
installs from validated argument arrays, challenges identical bytes on BASE and HEAD, self-rejects
weak evidence, permits one repair, persists content-addressed provenance, and can publish one
retry-safe GitHub Check through a GitHub App installation token. Repository child processes now
receive a credential-minimized environment, bounded output capture, and process-tree timeouts;
semantic calls have one explicit transient retry; and worker failures terminate durably with
sanitized codes. The credential-gated live claim smoke test has successfully exercised the real
Gemini integration.

PatchProof Bench replays four developer regression oracles from four genuine historical PRs across
`more-itertools` and `humanize`. All four reproduced BASE assertion failure / HEAD pass, and all
four controlled weak no-op candidates were rejected as non-discriminating. In this deliberately
small policy comparison, HEAD-only acceptance produced four false supports while the BASE/HEAD
policy produced none. A final explicitly authorized, bounded live Gemini workflow on
`more-itertools` PR 1223 selected a grounded claim, repaired one environmental candidate, and
produced an identical-artifact BASE assertion failure / HEAD pass. Semantic assessment returned
`CLAIM_SUPPORTED_FOR_SCENARIO`, matching the stored developer oracle's discriminating pattern.
The run used four provider attempts and 12,225 total tokens. It is real one-case workflow evidence,
not a blind candidate-generation benchmark: production retrieval included changed test context,
and the immutable historical commits required the benchmark's synthetic test contract.

The cloud composition is now live: transactional Firestore persistence, deterministic
OIDC-authenticated Cloud Tasks, a public webhook/control service, and an IAM-private executor with
no project roles or secrets. A pinned Python 3.12 shared container image, Cloud Build file,
least-privilege IAM, Secret Manager, scale-to-zero settings, and an exact PowerShell deployment
script are checked in. Cloud Build pushed image digest
`sha256:2ad74df4a335d6c4f4aa7d7aaaab8126658bd4fa0f4249278ad384d5f0838058`;
the control and executor revisions are Ready; public control liveness and authenticated private
executor liveness return HTTP 200; unauthenticated executor access returns HTTP 403; and Firestore
plus the rate-limited task queue are live in `asia-south1`. GitHub App 4711074 is active with the
required event and permissions. PR #1 produced signed `opened` and `synchronize` deliveries; the
successful immutable run `695eaa20-7db3-492f-a57e-9819ebb54087` traversed Cloud Tasks and the
private executor, persisted hash-verified evidence, observed BASE assertion failure / HEAD pass,
and published successful GitHub Check 97764451438. The first delivery also demonstrated honest
abstention after two invalid model-generated candidates. These live cases prove the deployed
composition, not an aggregate agent success rate. The live read-only
[evidence console](https://patchproof-control-q26kc4fdba-el.a.run.app/dashboard) now projects only
those two explicitly configured runs from Firestore, recomputes their evidence hashes, and shows
claim, candidate lineage, BASE/HEAD facts, abstention, and final GitHub result without publishing
raw model/process output or credentials. Hostile-code isolation is not implemented, and browser
screenshots remain pending because the available capture tool requires a newer Node runtime.

## Supported scope

The frozen hackathon scope is GitHub, explicitly allowlisted public repositories, pure Python,
pytest, and deterministic unit or small integration tests. PatchProof is not intended to prove
whole-PR correctness, replace review, or safely execute arbitrary malicious repositories.

## Local setup

Install Python 3.12 and [uv](https://docs.astral.sh/uv/), then run:

```shell
uv sync --frozen --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

On a machine where uv's global cache is unavailable, set `UV_CACHE_DIR` to a writable local
directory before running these commands.

To run the local Phase 2 control plane, provide a secret, allowlist, and database path, then start
the application factory:

```shell
uv run uvicorn patchproof.control_plane:create_app --factory
```

The factory reads `PATCHPROOF_WEBHOOK_SECRET`, `PATCHPROOF_ALLOWED_REPOSITORIES` (comma-separated
`owner/name` values), and optional `PATCHPROOF_DATABASE_PATH`. Do not commit real webhook secrets or
the local database.

To preview the evidence console with the checked sanitized live-proof fixture:

```shell
uv run python -m patchproof.dashboard_preview
```

Then open `http://127.0.0.1:8092/dashboard`. Production reads only the UUIDs explicitly listed in
`PATCHPROOF_DASHBOARD_RUN_IDS` and never accepts arbitrary dashboard record IDs from a request.

## Repository layout

```text
src/patchproof/  Python package
tests/           Automated tests
docs/            Product, architecture, decision, phase, and interview documentation
```

The source of truth for scope and claims is
[`docs/00_PROJECT_SPEC.md`](docs/00_PROJECT_SPEC.md).
The current control-plane design and failure semantics are taught in
[`docs/02_PHASE_GITHUB_CONTROL_PLANE.md`](docs/02_PHASE_GITHUB_CONTROL_PLANE.md).
The current context and claim-selection boundary is documented in
[`docs/03_PHASE_AGENT_CLAIMS.md`](docs/03_PHASE_AGENT_CLAIMS.md).
The candidate generation, contract, validation, and repair boundary is documented in
[`docs/04_PHASE_TEST_GENERATION.md`](docs/04_PHASE_TEST_GENERATION.md).
The orchestration, durable evidence, and GitHub Check boundary is documented in
[`docs/05_PHASE_EVIDENCE_WORKFLOW.md`](docs/05_PHASE_EVIDENCE_WORKFLOW.md).
The reliability limits, credential boundary, retry policy, and residual risk are documented in
[`docs/06_PHASE_RELIABILITY_SECURITY.md`](docs/06_PHASE_RELIABILITY_SECURITY.md).
The historical cases, methodology, raw results, metrics, and evaluation limitations are documented
in [`docs/07_PHASE_EVALUATION.md`](docs/07_PHASE_EVALUATION.md).
The deployed services, identity boundaries, exact commands, cost controls, and live end-to-end
proof are documented in
[`docs/08_PHASE_CLOUD_DEPLOYMENT.md`](docs/08_PHASE_CLOUD_DEPLOYMENT.md). The evidence-console
boundary, live release proof, four-minute demo, Devpost copy, screenshot plan, and final limitations
are documented in [`docs/09_PHASE_DASHBOARD_DEMO.md`](docs/09_PHASE_DASHBOARD_DEMO.md).

## Reproduce PatchProof Bench

```shell
uv run python -m patchproof.benchmark verify
uv run python -m patchproof.benchmark run
uv run python -m patchproof.benchmark summarize
```

The networked run clones only the two public repositories declared in the hash-checked manifest.
See [`benchmarks/results/summary.md`](benchmarks/results/summary.md) for the measured summary and
[`benchmarks/results/raw.json`](benchmarks/results/raw.json) for every retained scenario.
