# PatchProof

> Proof for the claim, not a verdict on the PR.

PatchProof is an event-driven GitHub agent intended to select one high-confidence behavioral
claim from an allowlisted Python/pytest pull request, construct bounded candidate regression
tests, challenge the identical test artifact against immutable BASE and HEAD revisions, reject
weak evidence, and publish a conservative claim-scoped report.

## Project status

**Phase 4 — bounded candidate-test generation and validation.** PatchProof reads
immutable BASE/HEAD Git objects, derives changed Python symbols with the AST, and ranks bounded
diff, source, test, import, and reference evidence. One logical stateless, tool-free Google ADK
agent using Gemini 3.6 Flash may select one grounded claim and propose one structured pytest
candidate. Deterministic validation enforces the repository's `.patchproof.yaml`, target path,
syntax, collection shape, imports, selected unsafe calls, immutable bytes, and artifact hash. One
repair is permitted, for an absolute maximum of two candidate model calls with explicit lineage.
The credential-gated live claim smoke test has successfully exercised the real Gemini integration.

The Phase 2 control plane, Phase 1 BASE/HEAD executor, and Phase 3–4 semantic pipeline remain
separate library slices. Phase 5 will orchestrate them, execute generated candidates, self-reject
weak evidence, and publish GitHub Checks. Cloud deployment and the dashboard are not implemented.

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
