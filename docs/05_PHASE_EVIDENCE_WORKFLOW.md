# Phase 5 - End-to-End Evidence Workflow and GitHub Check

**Status: COMPLETE.** Phase 5 connects an authenticated pull-request occurrence to immutable Git
context, one grounded claim or abstention, bounded candidate generation, validated dependency
installation readiness, identical BASE/HEAD replay, self-rejection and two repairs, constrained semantic
assessment, append-only evidence persistence, and one claim-scoped GitHub Check.

It does not deploy Cloud Tasks/Run/Firestore, clone arbitrary remote repositories, or claim a
hardened sandbox. Those remain later phases.

## Implemented flow

```text
authenticated PR webhook
  -> durable run + PR narrative + GitHub App installation ID
  -> idempotent dispatcher(run_id)
  -> immutable BASE/HEAD context and matching .patchproof.yaml
  -> claim selection or typed abstention
  -> BASE/HEAD repository setup readiness from the immutable contract
  -> candidate validation -> frozen install -> identical BASE/HEAD replay
  -> mechanical classification
       | discriminating -> bounded semantic relevance assessment
       | otherwise      -> at most two repairs, then abstain if still weak
  -> immutable evidence JSON + SHA-256
  -> terminal workflow result with publication=PENDING
  -> GitHub Check built only from stored evidence
```

The dispatcher is an injectable idempotent boundary. The webhook process does not run Gemini or
pytest inline. Cloud Tasks will implement that boundary in Phase 8.

## Self-rejection, repair, and abstention

The workflow permits one initial candidate and at most two repairs. A validated candidate is
replayed on BASE and HEAD. `NON_DISCRIMINATING`, `INVALID_TEST`, and `ENVIRONMENTAL` evidence cannot
become claim support. Only bounded status, pattern, and BASE/HEAD observations reach the repair
call; raw logs do not.

Every executed candidate remains in `candidate_evaluations`, including candidates rejected because
both revisions passed. If the third candidate also fails to distinguish the revisions, the result
is `INSUFFICIENT_EVIDENCE`; there is no fourth call or retry-until-green loop. A claim
abstention terminates before generation, installation, or pytest.

## Mechanical and semantic boundary

The mechanical classifier remains authoritative. The same logical stateless `patchproof_agent`
has a third narrow ADK task only after a pair is mechanically discriminating. It receives the
grounded claim, exact source, hashes, revision IDs, bounded statuses/details, and mechanical reason.
It has no tools or history.

Deterministic validation restricts the response:

- BASE assertion failure / HEAD pass may become claim-scoped support or insufficient evidence;
- BASE pass / HEAD assertion failure may become potential regression or insufficient evidence;
- an unrelated or uncertain assertion can only become insufficient evidence;
- semantic output cannot alter execution facts or describe the whole PR as verified.

## Immutable evidence provenance

One content-addressed document stores run/revision identity; claim, citations, usage and response
hash; up to three candidate attempts and immediate-parent lineage; source, validation issues, feedback and artifact
hashes; every executed candidate's BASE/HEAD facts, before/after hashes, bounded logs and mechanical
classification; final semantic usage/response hash; and the claim-scoped conclusion.

SQLite stores the JSON in one `evidence_records` row per run. An identical insert is idempotent;
different bytes for the same run raise `StoredEvidenceConflictError`. If a crash occurs after the
insert but before the terminal transition, executing again reconciles state from the stored result
without invoking Gemini or pytest.

## Validated installation and execution

The workflow reads `.patchproof.yaml` from both immutable Git trees, requires exact equality, and
requires the executor to hold that same parsed contract. Before any candidate-generation call, the
executor materializes BASE and HEAD and runs only their repository-declared, allowlisted setup argv.
BASE and HEAD setup failures have distinct stable readiness statuses and terminate as
`INSUFFICIENT_EVIDENCE`; they are never sent to the model as repair feedback. It derives the
complete bounded HEAD path set so generated source cannot overwrite a tracked file.

For each detached revision, the runner executes only the validated installation argument arrays,
without a shell, before candidate injection. Timeout, startup failure, or nonzero exit becomes
`ENVIRONMENT_SETUP_FAILED`, never assertion evidence or candidate-repair feedback. The stateless
remote executor revalidates setup for isolated challenge requests; cross-request environment
caching is intentionally not trusted. Phase 6 owns resource isolation and child-environment
minimization.

## GitHub App and Checks API

The webhook retains `installation.id`, title, and bounded body. The token provider signs a
ten-minute app JWT, calls GitHub's installation-token endpoint, and caches the short-lived token
until two minutes before expiry. Tokens and private keys are never stored in workflow evidence.

The client uses `POST /repos/{owner}/{repo}/check-runs`, PATCH by Check ID, commit Check listing for
ambiguous-create recovery, `Accept: application/vnd.github+json`, and API version `2026-03-10`.
The installation needs repository `Checks: write` permission.

The Check includes the claim, candidate/node/artifact hash, exact BASE and HEAD SHAs, mechanical
evidence, conclusion, bounded logs/source, and evidence hash. Claim support maps to GitHub
`success`, potential regression to `failure`, and insufficient evidence to `neutral`; all wording
stays explicitly scenario/claim scoped.

## Retry-safe publication

`check_publications` stores one payload hash, remote Check ID, attempt count, bounded error, and
timestamp. The run ID is GitHub's `external_id`. Publication loads immutable evidence, returns
immediately when already published, reuses a known remote ID, or discovers an ambiguous prior
create by name plus `external_id` before deciding to POST. Timeout/network/408/409/425/429/5xx are
retryable; other 4xx failures are terminal.

`GitHubCheckPublisher` has no model, repository, workspace, or pytest dependency. A publication
retry therefore cannot recompute evidence.

## Tests and observed result

The real integration test establishes BASE/HEAD setup readiness, self-rejects two both-pass
candidates, uses both repairs, observes BASE assertion failure / HEAD pass on the third candidate,
retains all three evaluations, persists evidence, and proves execution replay changes no
model/pytest counters. It
then simulates GitHub 503, recovers the ambiguous Check by `external_id`, PATCHes it, and proves an
already-published call makes no request. Separate coverage proves claim abstention and GitHub App
installation-token caching.

Current full suite: **302 passed, 1 credential-gated live Gemini test skipped, 2 upstream
warnings**.

Commands run from the repository root (with `UV_CACHE_DIR` directed to the ignored local cache):

| Command | Observed result |
| --- | --- |
| `uv lock --offline` | Lock resolved 64 packages; direct `httpx` and `google-auth` requirements recorded. |
| `uv sync --frozen --all-groups` | Editable environment synchronized successfully after approved access fetched a missing build helper. |
| `uv run ruff format --check .` | 54 files already formatted. |
| `uv run ruff check .` | All checks passed. |
| Phase 5 focused pytest command | 30 passed with two upstream warnings. |
| `uv run pytest -q` | 167 passed, 1 intentional live-test skip, 2 upstream warnings in 86.79 seconds. |
| `uv build` | Source distribution and wheel built successfully. |
| Distribution listing | Phase 5 modules/tests/docs present; local cache, venv, database, secrets, and test workspaces absent. |
| `git diff --check` | Passed. |

The first sandboxed sync/build attempts could not reach PyPI for isolated build dependencies. The
same declared operations succeeded with approved network access; no dependency rule was relaxed.

## Limitations

- The dispatcher is a protocol, not Cloud Tasks; no production worker is deployed.
- A caller supplies a trusted local clone containing the authenticated SHAs.
- SQLite remains local; Firestore and cloud transactions remain Phase 8.
- Constrained Python execution is not a hostile-code sandbox; hardening remains Phase 6.
- Secret Manager integration remains Phase 8. Static tokens are for explicit local/test use.
- GitHub artifact upload/annotations are not implemented; bounded logs and artifact provenance are
  embedded in the Check and evidence record.

## Interview questions

1. Why is non-discriminating execution a self-rejection rather than a failed PR?
2. Why can semantic assessment narrow but not override mechanical evidence?
3. How are first-candidate facts retained after repair?
4. What prevents publication retry from calling Gemini or pytest?
5. How does `external_id` close the ambiguous POST gap?
6. Why are publication and terminal execution separate workflow dimensions?
7. Why must BASE and HEAD use the same contract?
8. How does installation failure differ from assertion failure?
9. Why is a green Check not a whole-PR verdict?
10. Which boundaries remain local until deployment?

## Next phase

Phase 6 will harden reliability, security, resource limits, recovery, and observability. Phase 5
stops here.
