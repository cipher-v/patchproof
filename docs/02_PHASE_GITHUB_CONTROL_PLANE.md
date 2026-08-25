# Phase 2 — GitHub Control Plane and Workflow State

**Status: COMPLETE.** Phase 2 turns an authenticated, supported GitHub pull-request webhook into
a durable local verification-run record. It does not yet fetch PR context, call Gemini, dispatch
executor work, or publish a GitHub Check.

## Problem solved

The Phase 1 executor can challenge two immutable revisions, but it has no safe way to learn that a
pull request changed and no durable identity for work. Webhooks are delivered at least once, may
arrive out of order, and contain untrusted bytes. A useful control plane must authenticate the
exact bytes, reject work outside the product boundary, capture immutable revision identities, and
make retries or newer commits deterministic.

Phase 2 establishes that boundary. A valid public, allowlisted pull-request event produces one
stored run occurrence tied to a full BASE SHA and HEAD SHA. Duplicate deliveries resolve to the
same record. A later HEAD supersedes older work without erasing it.

## Implemented components

### FastAPI application factory

`patchproof.control_plane.create_app` creates the service from explicit settings and an injectable
`VerificationRunStore`. There is no database connection or secret-bearing application object at
module import time. Production can construct the default SQLite adapter from environment
settings; tests inject a temporary adapter.

The service exposes:

- `GET /healthz` and `GET /livez`, liveness endpoints independent of webhook credentials;
- `POST /webhooks/github`, the authenticated event-ingestion boundary.

`ControlPlaneSettings` requires a non-empty webhook secret, at least one valid canonical
`owner/repository` allowlist entry, a database path, and a positive body-size limit. Repository
matching is case-insensitive after canonicalization.

### Authenticated GitHub webhook boundary

The endpoint performs work in this order:

1. Stream the request body with a byte limit. An oversized declared or observed body receives
   `413`; malformed or negative `Content-Length` receives `400`.
2. Compute HMAC-SHA256 over the exact raw bytes and compare it with
   `X-Hub-Signature-256` using `hmac.compare_digest`. Missing or invalid authentication receives
   `401`. JSON is not parsed before authentication.
3. Validate the bounded `X-GitHub-Delivery` identifier and require `X-GitHub-Event`.
4. Acknowledge authenticated non-`pull_request` events without creating work.
5. Parse only the payload projection PatchProof needs, while ignoring unrelated GitHub fields.
6. Accept only `opened`, `reopened`, `synchronize`, and `ready_for_review` actions.
7. Reject private or non-allowlisted repositories with `403`.
8. Validate the PR number, timezone-aware `updated_at`, canonical repository, and full lowercase
   40- or 64-hex BASE/HEAD SHAs before calling storage.

The response intentionally exposes only disposition, explanation, run ID, lifecycle, and revision
state—not the whole durable record or any secret-bearing data. A new or stale recorded occurrence
returns `202`; an idempotent replay returns `200`; an irrelevant authenticated event returns
`202 IGNORED`.

### Durable workflow model

`workflow.py` models independent concerns instead of compressing them into one misleading status:

| Dimension | Values now | Question answered |
| --- | --- | --- |
| Lifecycle | `ACCEPTED`, `QUEUED`, `RUNNING`, `TERMINAL` | Is work schedulable, active, or finished? |
| Phase | context through publication | Which business step has been reached? |
| Terminal reason | completed, failed, superseded, cancelled | Why did active work stop? |
| Mechanical evidence | Phase 1 evidence enum or unset | What did execution mechanically show? |
| Claim outcome | conservative claim enum or unset | What semantic conclusion was recorded? |
| Publication | not started, pending, published, retryable or terminal failure | Can result publication be retried independently? |
| Revision | current or superseded | Does this occurrence represent the newest known PR HEAD? |

This separation makes otherwise impossible states expressible without lying. For example, a run
can be `TERMINAL/COMPLETED` while publication is in `RETRYABLE_FAILURE`, so retrying a GitHub API
call does not imply rerunning Gemini or pytest. A completed historical run can later be marked
`SUPERSEDED` in the revision dimension while retaining `COMPLETED` as its original terminal
reason.

`RunTransition` and `apply_run_transition` enforce deterministic rules:

- lifecycle and phase cannot move backward;
- terminal lifecycle and terminal reason must agree;
- evidence and claim outcomes are immutable once recorded;
- publication can retry only from its retryable state and cannot leave a final state;
- superseded runs cannot resume or publish;
- audit timestamps cannot move backward;
- a transition that changes nothing is rejected;
- every successful update increments an optimistic version.

The model already reuses Phase 1's evidence and claim enums, but Phase 2 does not manufacture an
evidence result.

### Storage abstraction and SQLite adapter

`VerificationRunStore` is the control plane's narrow persistence contract: accept a PR event, get
one run, list a PR's history, or apply an optimistic transition. The FastAPI layer does not depend
on SQL. A later Firestore implementation can implement the same behavioral contract, although
Firestore transaction and indexing details will differ.

`SqliteVerificationRunStore` is a durable local adapter. It creates one connection per operation,
closes it deterministically, enables foreign keys, and uses `BEGIN IMMEDIATE` for write
transactions. That transaction mode serializes competing writers before they make idempotency or
current-revision decisions.

The local schema has:

- `runs`: immutable identity fields plus the independent state dimensions, timestamps, links, and
  optimistic version;
- `deliveries`: a unique GitHub delivery ID mapped to the run occurrence that handled it;
- a partial unique index allowing at most one `CURRENT` run per repository and PR;
- lookup indexes for PR history and revision replay.

SQLite's ordinary durable database file meets local Phase 2 needs without adding an external
service. Schema migrations and cloud concurrency are intentionally deferred.

## Idempotency and supersession algorithm

Acceptance is one atomic transaction. The cases are:

1. **Known delivery ID:** return the previously mapped run as `DUPLICATE_DELIVERY` regardless of a
   redelivered body's content. GitHub delivery identity is the first replay key.
2. **Same revision occurrence under another delivery:** when the BASE/HEAD pair is still current,
   or an out-of-order observation is no newer than the current occurrence, map the new delivery to
   the existing occurrence and return `DUPLICATE_REVISION`.
3. **Newer, distinct HEAD:** mark the previous current occurrence `SUPERSEDED`. If it was active,
   terminate it with reason `SUPERSEDED`; if it was already terminal, preserve its original
   reason. Create a new `CURRENT/ACCEPTED` run.
4. **Out-of-order distinct HEAD:** persist it as a terminal superseded occurrence linked to the
   current run. Return `STALE_CREATED`; do not enqueue it or displace current work.
5. **Later return to a previously seen SHA:** create a new occurrence when its authenticated PR
   `updated_at` is newer than the current occurrence. Git history can revisit the same commit, so
   revision bytes alone are not a globally unique occurrence identity.

This gives exactly-once *record creation semantics for ordinary replays*, not a claim of
exactly-once distributed execution. Later externally visible actions must still be idempotent.

Ordering uses the webhook payload's timezone-aware pull-request `updated_at`, because GitHub
delivery IDs are identifiers rather than sequence numbers. If two distinct HEADs have exactly the
same timestamp, PatchProof conservatively keeps the first observed one current and records the
other as stale; without another causal field, claiming which is newer would be guesswork.

## Control and data flow

```text
raw HTTP bytes
      |
      v
bounded stream read -> HMAC-SHA256 verification
      |
      v
delivery/event validation -> action/public/allowlist filtering
      |
      v
minimal Pydantic payload -> immutable BASE/HEAD event identity
      |
      v
SQLite BEGIN IMMEDIATE
      |
      +--> delivery replay? --------> existing run
      |
      +--> revision replay? --------> existing occurrence
      |
      +--> stale/new/returned HEAD -> audit + current/supersession update
      |
      v
small HTTP acknowledgement
```

The Phase 1 executor remains a separate local library. Phase 2 records work but deliberately does
not invoke that library synchronously from a webhook request; orchestration and dispatch belong to
later phases.

## Failure cases and behavior

| Condition | Result | Durable mutation? |
| --- | --- | --- |
| Oversized body | `413` | No |
| Missing/invalid HMAC | `401` | No |
| Missing/malformed delivery ID or event header | `400` | No |
| Authenticated non-PR event or unsupported PR action | `202 IGNORED` | No |
| Malformed PR payload, naive time, partial SHA | `400` | No |
| Private or non-allowlisted repository | `403` | No |
| New current revision | `202 ACCEPTED` | New run and delivery |
| Exact delivery replay | `200 DUPLICATE` | No new run |
| Same revision, different delivery | `200 DUPLICATE` | Delivery mapped to existing run |
| Older out-of-order revision | `202 STALE` | Auditable terminal occurrence |
| Stale optimistic version | exception at store boundary | Transaction rolled back |
| Illegal state transition | exception at store boundary | Transaction rolled back |

Unexpected storage failures remain server errors rather than being misreported as successful
acceptance.

## Alternatives and trade-offs

- **SQLite instead of JSON files:** SQLite supplies atomic transactions, uniqueness, indexes, and
  crash-safe persistence. A hand-written JSON store would need to recreate locking and atomic
  replacement badly. Firestore is deferred until cloud deployment so local work has no service or
  cost dependency.
- **Protocol instead of a generic repository framework:** four typed methods are enough. A broad
  ORM or cloud abstraction would hide important transaction semantics without making Firestore
  interchangeable automatically.
- **Independent state dimensions instead of one enum:** more combinations exist, but each field
  has one meaning and publication retries cannot restart execution accidentally.
- **Occurrence records instead of globally unique SHA pairs:** returning to an earlier Git commit
  is possible. Keeping each later occurrence preserves current-state correctness and history;
  ordinary revision replays still deduplicate.
- **Pydantic boundary projections instead of the full GitHub schema:** validation stays typed while
  remaining tolerant of GitHub adding unrelated fields.
- **Synchronous local storage behind an interface:** simple and adequate for local ingestion. The
  route and adapter are not yet tuned for high-throughput cloud operation.

## Tests and what they prove

Phase 2 adds four focused test modules:

- `test_github_webhook.py` proves exact-byte HMAC behavior and delivery-ID validation, including
  malformed schemes and digests.
- `test_control_plane.py` proves the full HTTP boundary: all supported actions, ignored events,
  malformed authenticated input, allowlist/private rejection, body limits, both replay keys,
  stale delivery handling, and new-HEAD supersession.
- `test_workflow.py` proves the state dimensions remain independent and that illegal lifecycle,
  phase, evidence, publication, timestamp, and superseded-run transitions fail.
- `test_storage.py` proves disk persistence across adapter recreation, transaction-backed replay
  behavior, simultaneous revision acceptance, optimistic concurrency, preserved terminal reasons,
  out-of-order audit records, and return-to-prior-SHA occurrences.

The Phase 2-focused suite contains 47 tests. The complete Phase 0–2 suite contains 99 tests,
including Phase 1's real temporary Git repositories and subprocess pytest executions. The full
suite proves the components coexist; it does not yet prove webhook-to-executor orchestration,
Firestore behavior, Gemini output, or GitHub publication.

## Commands and observed results

Commands were run from the repository root with uv's cache directed to the ignored workspace-local
`.uv-cache` because the managed sandbox cannot use the normal user cache.

| Command | Observed result |
| --- | --- |
| `uv sync --frozen --all-groups` | All locked runtime and development dependencies synchronized on Python 3.12. |
| `uv run pytest tests/test_github_webhook.py tests/test_workflow.py tests/test_storage.py tests/test_control_plane.py -q` | 47 Phase 2 tests passed; one upstream TestClient deprecation warning. |
| `uv run pytest -q` | 99 tests passed; one upstream TestClient deprecation warning. |
| `uv run ruff check .` | All checks passed. |
| `uv run ruff format --check .` | All files formatted. |
| `uv lock --check` | Lockfile current. |
| `uv build` | Source distribution and wheel built successfully. |

During development, all initial Phase 2 assertions passed but Windows refused to remove temporary
SQLite files. Python's SQLite connection context manager commits or rolls back but does not close
the connection; using `contextlib.closing` fixed the leaked handles. This is recorded because the
same detail matters for long-running resource hygiene, not just test cleanup.

The first final `uv build` attempt also failed because its isolated build environment tried to
resolve Hatchling from PyPI while the managed sandbox blocked network access. Repeating the same
build with approved network access succeeded; no source, lockfile, or dependency change was needed.

## Security position

Implemented safeguards are narrow: exact-byte webhook authentication, bounded input, typed
payload validation, public-repository enforcement, and an explicit allowlist. This phase does not
make repository execution safe, authenticate a GitHub App installation, provide rate limiting,
isolate tenants, or protect a deployed SQLite file. Repository-controlled code is not executed by
this endpoint.

## Limitations and remaining work

- Storage is local SQLite with no schema-migration framework, backup policy, or multi-instance
  deployment contract. Firestore remains a Phase 8 adapter.
- `pull_request.updated_at` is the best available ordering signal in the current projection, but
  equal timestamps for distinct heads are causally ambiguous.
- Accepted work is not queued and the Phase 1 executor is not invoked.
- There is no PR diff/context retrieval, claim selection, ADK/Gemini call, generated test, or
  semantic assessment.
- No GitHub App installation authorization, Checks API publication, publication idempotency key,
  or structured operational event log exists yet.
- The synchronous local adapter is designed for correctness and testability, not high-throughput
  multi-process serving.
- The TestClient stack currently emits an upstream Starlette deprecation warning about `httpx`;
  application tests still pass, and changing to an unrelated pre-release client was not justified.

## Interview questions to answer

1. Why must HMAC be checked against raw bytes before JSON parsing?
2. Why are delivery replay and revision replay two different idempotency cases?
3. How does `BEGIN IMMEDIATE` protect the read-decide-write acceptance sequence?
4. Why does a returned historical SHA need a new run occurrence?
5. What is the difference between lifecycle `TERMINAL`, terminal reason `COMPLETED`, and revision
   state `SUPERSEDED`?
6. Why can publication retry without changing execution lifecycle?
7. What does optimistic versioning prevent, and what does it not guarantee?
8. Which SQLite semantics can a Firestore adapter preserve, and which mechanisms must change?
9. Why is an out-of-order event retained instead of silently dropped?
10. What security properties does this endpoint implement, and which does it explicitly not
    claim?

## Next phase

Phase 3 will add deterministic PR context retrieval and the first bounded ADK/Gemini behavioral
claim agent. It has not started.
