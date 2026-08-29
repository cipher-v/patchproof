# PatchProof Project Specification and Phase 0 Notes

## Document status

This document is the source of truth for the product boundary as of Phase 7. Sections describing
the target system are design commitments, not claims that those features are already implemented.
Implemented behavior consists of the Python project foundation described under "Phase 0
implementation," the deterministic executor documented in
[`01_PHASE_BASE_HEAD_EXECUTOR.md`](01_PHASE_BASE_HEAD_EXECUTOR.md), and the authenticated local
control plane and durable workflow state documented in
[`02_PHASE_GITHUB_CONTROL_PLANE.md`](02_PHASE_GITHUB_CONTROL_PLANE.md). It now also includes the
bounded deterministic context retriever and structured claim-selection agent documented in
[`03_PHASE_AGENT_CLAIMS.md`](03_PHASE_AGENT_CLAIMS.md), plus the execution contract and bounded
candidate generation/repair boundary documented in
[`04_PHASE_TEST_GENERATION.md`](04_PHASE_TEST_GENERATION.md), the end-to-end evidence workflow in
[`05_PHASE_EVIDENCE_WORKFLOW.md`](05_PHASE_EVIDENCE_WORKFLOW.md), and the reliability/security
boundary in [`06_PHASE_RELIABILITY_SECURITY.md`](06_PHASE_RELIABILITY_SECURITY.md).
The reproducible historical executor/policy benchmark and its explicit agent-evaluation limits are
documented in [`07_PHASE_EVALUATION.md`](07_PHASE_EVALUATION.md).

## Problem

A green existing test suite does not necessarily demonstrate that a pull request reproduced and
fixed the behavior described by its author. Reviewers often lack executable evidence that a
specific claimed behavior differs between the old and proposed revisions. Generated tests make
this worse if they are accepted merely because they pass on the new revision: a test that also
passes on the old revision proves no change.

PatchProof addresses that evidence gap. It attempts to construct and challenge one bounded
regression-test artifact for one selected behavioral claim.

## Target user

The primary user is a developer or open-source maintainer reviewing a Python/pytest pull request.
The user wants concise, auditable evidence about a concrete claimed behavior without being given
a false whole-PR verdict.

## Product thesis

> PatchProof is an event-driven GitHub agent that selects one high-confidence behavioral claim
> from an allowlisted Python/pytest pull request, constructs bounded candidate regression tests,
> challenges those tests against immutable BASE and HEAD revisions, rejects invalid or
> non-discriminating evidence, and publishes a conservative, claim-scoped evidence report.

Tagline: **Proof for the claim, not a verdict on the PR.**

The differentiator is the integrated, auditable workflow from a real PR event through bounded
semantic selection, identical BASE/HEAD test replay, mechanical validation, self-rejection,
durable state, and a reviewer-facing report. BASE/HEAD fail-to-pass testing itself is not claimed
as novel research.

## Frozen hackathon scope

PatchProof supports only:

- GitHub pull requests from public, explicitly allowlisted repositories;
- pure-Python projects using Python 3.12 and pytest;
- deterministic unit tests or small integration tests;
- one primary behavioral claim per verification run;
- at most three generated candidate tests: one initial attempt and at most two repairs;
- execution commands taken from a validated repository execution contract;
- conservative abstention when evidence is insufficient.

Strong counterfactual conclusions require sufficiently comparable BASE and HEAD interfaces and
environments. A dependency- or environment-changing pull request does not receive a strong
differential conclusion unless that comparability can be established.

## Non-goals

The hackathon version will not:

- prove the correctness of an entire pull request;
- replace code review or act as an enterprise CI system;
- support arbitrary languages, test frameworks, or private repositories;
- guarantee safe execution of arbitrary malicious repositories;
- require databases or external services for tests;
- permit an LLM to invent or execute arbitrary shell commands;
- use embeddings, a vector database, or broad repository ingestion without a demonstrated need;
- introduce a multi-agent architecture, Kubernetes, Kafka, Temporal, or Pub/Sub without a concrete
  requirement and explicit approval;
- prematurely build post-hackathon features such as TypeScript support or full observability.

## Evidence philosophy

The identical candidate-test bytes and test identifier must be challenged against immutable BASE
and HEAD revisions. A BASE failure and HEAD pass can be claim-consistent, but it is not sufficient
by itself: import failures, collection errors, dependency failures, fixture errors, skips, xfails,
timeouts, or unrelated exceptions can create the same surface pattern.

PatchProof therefore keeps two concepts separate:

1. **Mechanical evidence**, determined by software from execution facts:
   `DISCRIMINATING`, `NON_DISCRIMINATING`, `INVALID_TEST`, `ENVIRONMENTAL`, or
   `COUNTERFACTUAL_NOT_APPLICABLE`.
2. **Claim-level outcome**, conservatively scoped to the tested scenario:
   `CLAIM_SUPPORTED_FOR_SCENARIO`, `CLAIM_NOT_SUPPORTED_FOR_SCENARIO`,
   `POTENTIAL_REGRESSION`, or `INSUFFICIENT_EVIDENCE`.

The LLM may help interpret semantics but may not override mechanical evidence. A candidate that
passes on both BASE and HEAD is deliberately rejected as non-discriminating. Insufficient evidence
is a valid product result, not a system failure.

## System boundary and responsibility split

The target system uses one bounded Google ADK agent for semantic choices: selecting a claim,
ranking deterministic context, constructing a test, revising it from immediately preceding bounded
feedback at most twice, deciding whether a failure appears related to the claim, and abstaining.

Deterministic code owns trust-boundary work: webhook HMAC validation, repository allowlisting,
immutable SHA capture, idempotency, workspace management, artifact hashing, command validation,
process execution, timeouts, pytest parsing, mechanical classification, retry budgets, stale-run
handling, and GitHub Check publication.

Context retrieval starts with `git diff`, Python AST analysis, changed symbols, imports/references,
paths, and nearby tests. It does not send an entire repository to Gemini.

Repository execution will be governed by a validated `.patchproof.yaml` contract specifying the
Python version, predefined installation and test commands, allowed test paths, and a timeout. This
contract is implemented in Phase 4 as a strict, bounded YAML mapping whose commands are argument
arrays selected from deterministic templates. The model never receives or supplies those command
values. Phase 5 executes those validated arrays on both revisions as part of orchestration.

As of Phase 6, deterministic code implements GitHub webhook HMAC validation, bounded payload
handling, public-repository allowlisting, full BASE/HEAD SHA capture, delivery/revision
idempotency, local SQLite persistence, explicit state transitions, and stale/superseded revision
history. Phase 1's BASE/HEAD executor is also implemented. Deterministic Git/AST context retrieval
and one schema-constrained, tool-free Google ADK claim agent are implemented as a separate local
slice. The agent uses an explicit Gemini 3.6 Flash model and must select at most one grounded claim
or abstain; its live integration test has completed successfully. The same logical agent can now
propose a structured pytest candidate through a separate task schema. Deterministic code validates
the candidate path, syntax, test shape, imports, selected unsafe calls, immutable content hash, and
at-most-three/two-repair lineage against `.patchproof.yaml`. Local orchestration now connects claim,
candidate, install, BASE/HEAD execution, self-rejection/repair, constrained assessment, immutable
evidence persistence, GitHub App installation authentication, and retry-safe Check publication.
Repository processes now use an explicit credential-minimized environment, bounded streaming
output, and process-tree timeouts. Semantic tasks retry once only for explicit transient provider
failures, and worker exceptions become sanitized durable terminal failures. Cloud task dispatch,
Firestore, deployed repository lifecycle, and hostile-code isolation remain target behavior rather
than current claims.

## Target architecture

The intended cloud path is:

```text
GitHub App / webhook
        -> Cloud Run control plane
        -> Firestore durable workflow state
        -> Cloud Tasks directed execution request
        -> Cloud Run executor
        -> Firestore evidence/result
        -> control plane
        -> GitHub Check
```

The control plane will own GitHub write credentials, ADK/Gemini calls, orchestration, task
creation, state transitions, and publication. The executor will own checkout and bounded command
execution and will not receive GitHub write or model credentials. Cloud Tasks is preferred because
the workload is directed asynchronous work, not event fan-out.

Workflow state will use separate lifecycle, current-phase, terminal-reason, evidence-outcome, and
publication dimensions. Publication retries must not repeat Gemini calls or test execution, and a
new HEAD must supersede older work while preserving its audit record.

## Hackathon requirements and cost boundary

The final submission targets the Google All Things Agentic Hackathon Taskmaster track and must use
Gemini 3.5 or newer, Google ADK or another permitted Google agent framework, and at least one
qualifying Google Cloud infrastructure service visible in the demo.

The target out-of-pocket cost is INR 0. Development remains local until cloud deployment is
required. The project will avoid unnecessary services, paid APIs, domains, commercial databases,
and premium tooling. Model and token usage will be tracked where practical.

## Security positioning

The intended honest claim is: configured allowlisted repositories execute in ephemeral,
constrained workers with least-privilege identities and bounded execution. Planned safeguards
include validated commands and paths, minimal child-process environments, time and resource
limits, log truncation, artifact hashes, and credential separation.

PatchProof will not claim a hardened hostile-code sandbox, full network isolation, arbitrary
malicious-repository safety, complete supply-chain security, or enterprise-grade isolation.

## Honest public claims

Acceptable claims:

- PatchProof gathers and challenges executable evidence for one specific PR claim.
- It replays identical candidate tests on immutable BASE and HEAD revisions.
- It rejects invalid or non-discriminating evidence and can abstain.

Prohibited claims:

- PatchProof proves PR correctness or guarantees bug-free code.
- PatchProof replaces human review or enterprise CI.
- PatchProof safely runs arbitrary malicious repositories.
- PatchProof understands every repository.

## Evaluation status and direction

Phase 7 now contains four genuine historical bug-fix PRs across `more-itertools` and `humanize`,
with immutable BASE/HEAD SHAs and hash-checked developer regression oracles held outside the
historical repositories. The recorded run reproduced all four oracle differentials, rejected four
controlled weak/no-op candidates, and retained raw results plus explicit false-support
denominators. It also ran 18 controlled failure/recovery cases selected by nine explicit test
nodes.

Those results measure reference-oracle replay and deterministic evidence policy. They do not
measure live Gemini candidate generation, semantic accuracy, or a representative production false
support rate. Those metrics remain future measured work and must retain all attempts without
cherry-picking.

## Hackathon and post-hackathon boundary

The hackathon version focuses on one polished end-to-end workflow, reliable self-rejection,
failure handling, a credible benchmark, Google-native deployment, and an evidence-focused demo.
Potential later extensions include TypeScript/Jest, a larger benchmark, stronger isolation,
PostgreSQL, OpenTelemetry, richer GitHub App installation, and optional generated-test PRs. These
are not part of current implementation scope.

## Phase 0 implementation

### Problem solved

Phase 0 establishes a reproducible, inspectable foundation so later evidence logic is not built on
ad hoc packaging or undocumented claims.

### Implemented

- a Python `src/` package constrained to Python 3.12;
- uv project metadata and a locked development dependency set;
- pytest discovery and strict configuration;
- Ruff linting and formatting configuration;
- a package import/metadata smoke test;
- ignore rules for environments, caches, build output, local configuration, and editor files, plus
  repository-wide LF line-ending normalization;
- a minimal README and the complete phase-document structure;
- initial architecture, decision, and interview documentation.

The only runtime data exposed by the package is `patchproof.__version__`. There are no domain
models, services, agents, executors, or cloud components yet.

### Control and data flow

For Phase 0, uv reads `pyproject.toml` and `uv.lock`, creates the environment, and installs the
package from `src/patchproof`. Pytest imports that installed package. The smoke test compares the
package's public version with installed distribution metadata, proving the src layout and build
metadata are connected rather than accidentally importing from the repository root.

### Failure cases covered

The tool configuration fails fast for unsupported Python versions, invalid pytest configuration,
unknown markers, lint violations, formatting drift, and package/metadata import mismatch. It does
not yet cover any product behavior.

### Alternatives and trade-offs

- Python 3.12 is selected over 3.14 for current ecosystem compatibility and hackathon
  reproducibility.
- A `src/` layout is selected over a flat package so tests exercise the installed package.
- uv is selected for environment and lockfile management; Hatchling is only the minimal PEP 517
  build backend.
- Ruff plus pytest is intentionally smaller than a broad early toolchain. A dedicated type checker
  can be added when typed domain behavior exists and provides meaningful value.

### Commands

The validated Phase 0 commands and their observed results are recorded in "Phase 0 verification"
below. Standard local use is:

```shell
uv sync --frozen --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

### Phase 0 verification

All final commands ran from the repository root with uv's cache redirected to the ignored,
workspace-local `.uv-cache` directory because the managed development sandbox could not access
uv's normal user cache.

| Command | Observed result |
| --- | --- |
| `uv sync --frozen --all-groups` | Created a Python 3.12 environment and installed the 8 locked packages. |
| `uv run python --version` | `Python 3.12.6`. |
| `uv run pytest` | Collected 1 test; 1 passed. |
| `uv run ruff check .` | All checks passed. |
| `uv run ruff format --check .` | 16 files already formatted. |
| `uv lock --check` | Lockfile resolved with no changes. |
| `uv build` | Built `patchproof-0.1.0.tar.gz` and `patchproof-0.1.0-py3-none-any.whl`. |
| archive content inspection | Confirmed `.uv-cache` and `.venv` were absent from both distributions. |
| `git diff --check` | No whitespace errors; because the empty remote has no initial commit, a temporary alternate-index `git diff --cached --check` also checked all 21 new files. |
| `git status --short --branch` | No commits exist yet; only the intended Phase 0 files are untracked. |

The first sandboxed pytest invocation passed the test but warned that it could not atomically
create its optional cache directory. Pre-creating the ignored `.pytest_cache` directory removed
the environment-specific warning; the final test run was clean. The initial concurrent Ruff
format check encountered that incomplete cache directory and crashed; after cleanup, an isolated
format run identified two trailing blank lines, Ruff formatted them, and the final format check
passed. These transient tool-environment failures are recorded rather than hidden; neither was a
product-test failure.

### Phase 0 limitations at completion

The repository has no BASE/HEAD executor or evidence model. Phase 1 will add the first product
vertical slice: immutable revision workspaces, identical artifact replay, pytest result capture,
timeouts, and deterministic mechanical evidence classification. That work must not begin until
explicit approval.

### Interview questions for this phase

1. Why pin the project to Python 3.12 rather than accepting newer interpreters?
2. What does the src layout prevent that a flat repository layout can hide?
3. What does the Phase 0 smoke test prove, and what does it explicitly not prove?
4. Why are product claims separated from target architecture and current implementation status?
5. Why is a small toolchain preferable before domain code exists?
