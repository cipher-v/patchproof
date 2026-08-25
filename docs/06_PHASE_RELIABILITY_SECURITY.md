# Phase 6 — Reliability and Security Hardening

**Status: COMPLETE.** Phase 6 hardens the local worker and subprocess boundary. It does not claim
that PatchProof is a hostile-code sandbox or an enterprise CI platform.

## Problem solved

The Phase 5 workflow was functionally complete, but two implementation details were too trusting:
repository-controlled installation and pytest processes inherited the worker's full environment,
and their output was captured without a memory bound before later report truncation. A workflow
exception could also leave a run in `RUNNING`, and transient model failures had no explicit retry
policy.

Phase 6 adds deterministic limits and durable failure semantics around those boundaries while
preserving the product rule that lack of evidence is not success.

## Implemented controls

### Minimal child-process environment and credential separation

`ChildProcessEnvironmentPolicy` constructs a new environment instead of copying the worker
environment. It inherits only operational values needed to start Python and validate TLS:

- `PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`, and `COMSPEC` where present;
- `LANG`, `LC_ALL`, `SSL_CERT_FILE`, and `REQUESTS_CA_BUNDLE` where present.

It creates per-execution home, temporary, and uv-cache directories and supplies fixed
non-interactive Python/pytest settings. Variables such as `GOOGLE_API_KEY`,
`GOOGLE_APPLICATION_CREDENTIALS`, GitHub tokens, the GitHub App private key, and webhook secrets
are not copied. Dependency installation and pytest use this same policy.

This is credential minimization, not process isolation. Repository code can still access the
network and host resources made available to the worker runtime. Phase 8 must configure a
least-privilege deployed executor with no model or GitHub write credentials.

### Bounded subprocesses

`BoundedSubprocessRunner` launches validated argument vectors with `shell=False`, closes stdin,
and drains stdout and stderr concurrently. Each stream retains at most 12,000 characters by
default and receives an explicit truncation marker. The collector continues draining discarded
bytes so a noisy child cannot block on a full pipe, while PatchProof never retains an unbounded
log in memory.

The execution-contract timeout remains capped at 300 seconds. On expiry, PatchProof terminates the
process group/tree and uses a direct parent-process kill as a fallback. Start failures expose only
a stable generic message; operating-system paths and exception prose are not evidence.

These controls bound elapsed test/install subprocess time and retained output. They do not impose
CPU, memory, filesystem, syscall, or network quotas.

### Bounded transient model retry

Each logical claim, candidate, or final assessment invocation gets at most two provider attempts:
the initial call and one retry. A retry is allowed only for explicit timeout, transport, provider
5xx, throttling, and selected conflict/deadline statuses. Malformed structured output, grounding
failure, ordinary 4xx responses, and deterministic validation failures are not retried.

The candidate policy is still separate: one initial candidate and at most one repair. A provider
retry repeats the same logical call; it does not create another candidate or an agent-directed
loop. `ModelUsage.provider_attempts` records whether one or two provider attempts were consumed.

### Durable fail-closed worker errors

`ReliableEvidenceWorker` is the top-level local worker boundary. If the workflow raises, it maps
the exception to a stable error code and bounded operator-safe summary. For a current nonterminal
run, SQLite atomically transitions the run to terminal `FAILED` and inserts one immutable
`run_failures` record containing the phase, code, retryability, summary, and timestamp. Invalid
claim output additionally retains typed model usage and the raw-response SHA-256 for audit; the
same optional fields are represented by Firestore.

The first failure record wins on replay. Provider response text, repository-controlled exception
text, credentials, and stack traces are not stored in that record or returned in the public
worker error. A terminal run without stored evidence cannot silently execute again.

Retryability is recorded for future orchestration; Phase 6 does not automatically create another
run occurrence. Evidence outcomes such as `INSUFFICIENT_EVIDENCE` remain successful terminal
product results and are not worker failures.

## Failure matrix

| Condition | Implemented behavior |
|---|---|
| Duplicate webhook delivery | Acknowledge the existing run; do not dispatch a second run. |
| Duplicate current BASE/HEAD revision | Reuse the current occurrence; preserve delivery audit. |
| Older or stale HEAD event | Record/acknowledge it without replacing or executing the current revision. |
| Newer HEAD during active work | Mark the old occurrence terminal and superseded; it cannot resume or publish. |
| Worker/subprocess timeout | Terminate the process tree, retain bounded facts, classify execution conservatively. |
| Invalid candidate/path/import/call | Reject before workspace execution; at most one bounded repair is possible. |
| Malformed or ungrounded model output | Fail closed without a provider retry. |
| Explicit transient model failure | Retry the identical logical call once, then surface a sanitized failure. |
| GitHub 408/409/425/429/5xx or network failure | Mark publication retryable; retry only stored evidence publication. |
| GitHub permanent 4xx or missing installation ID | Mark publication terminal; do not retry automatically. |
| Executor/start/workspace failure | Preserve bounded execution facts or a sanitized durable worker failure. |
| Excess process output | Drain it, retain only the configured prefix, and mark it truncated. |

## Prompt-injection boundary

PR prose, diffs, source, tests, comments, and generated candidate source are untrusted data. They
may contain text such as “ignore the system” but cannot add tools, commands, paths, or policy.
The ADK tasks have no tools and receive explicit schemas. Deterministic code validates claim
citations, candidate paths and ASTs, command templates, immutable artifact hashes, and mechanical
BASE/HEAD results. The final semantic task can narrow a conclusion but cannot override mechanics.

This reduces authority available to prompt injection. It is not a guarantee that a model can
never be influenced by adversarial text, so strict validation, abstention, and bounded attempts
remain required.

## Execution and trust budgets

The relevant hard ceilings are:

- authenticated webhook body and repository allowlist limits from Phase 2;
- bounded diff, file, snippet, reference, and serialized context limits from Phase 3;
- strict candidate input/output/source/path budgets, two candidate calls, and one repair;
- at most two provider attempts per logical semantic call and adapter request timeouts;
- validated installation/test argv templates and a repository timeout of at most 300 seconds;
- bounded per-stream subprocess capture before any evidence object is created;
- bounded persisted error summaries and GitHub Check output.

There is intentionally no “retry until green,” open-ended agent conversation, shell command
construction, or unbounded retained child output.

## Verification

The Phase 6 tests cover environment allowlisting, credential exclusion, isolated runtime paths,
large stdout/stderr, process timeout and start failure, transient/permanent model errors, exact
attempt ceilings, durable sanitized worker failure, failure idempotency, GitHub retry/terminal
classification, and credential-safe publication errors. Earlier tests continue to cover duplicate
delivery, stale/new HEAD ordering, supersession, prompt-injection-shaped repository text,
candidate validation, path rules, command templates, and identical BASE/HEAD replay.

Final local verification on Windows/Python 3.12:

```text
uv run pytest -q
186 passed, 1 skipped, 2 warnings in 96.01s

uv run ruff check src tests
All checks passed!
```

The skipped test is the opt-in billable live Gemini smoke test. It had already passed separately
with credentials; Phase 6 uses deterministic adapter tests and does not spend another live call.
The warnings are upstream ADK/Starlette deprecations, not test failures.

## Honest residual risk

PatchProof still executes repository dependency and test code on the host worker. The current
implementation does not provide container/VM isolation, outbound-network denial, read-only root
filesystems, syscall filtering, CPU or memory quotas, complete supply-chain defense, or protection
against every indirect Python behavior. The AST policy catches selected unsafe constructs but is
not a security proof.

The accurate claim is narrower: execution accepts only validated commands and paths, uses a
credential-minimized environment, has bounded time and retained output, keeps immutable artifact
identity, fails closed with sanitized durable state, and separates publication retries from
expensive work. Stronger deployed isolation remains Phase 8 work.
