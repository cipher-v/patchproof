# Architecture Decisions

This is a lightweight ADR log. "Planned consequence" means the decision constrains later phases
but has not necessarily been implemented yet.

## ADR-001 — Use Python 3.12, not Python 3.14

- **Context:** Google ADK, Google Cloud libraries, parsing tools, pytest plugins, and historical
  benchmark projects must work reproducibly.
- **Choice:** Require Python `>=3.12,<3.13` and record `3.12` in `.python-version`.
- **Why:** Python 3.12 provides mature ecosystem support while retaining modern typing and runtime
  features.
- **Alternative rejected:** Python 3.14, because early adoption raises avoidable dependency and
  benchmark compatibility risk.
- **Consequence:** Development and CI must fail rather than silently use a different minor version.

## ADR-002 — Use one bounded agent, not a multi-agent system

- **Context:** Semantic claim selection and test construction need model reasoning, while most
  workflow work is deterministic.
- **Choice:** Plan one Google ADK agent with explicit structured tasks and budgets.
- **Why:** One agent keeps provenance, costs, failure handling, and interview explanations clear.
- **Alternative rejected:** Multiple role-labelled agents, because they would add orchestration
  without a demonstrated independent concurrency or specialization need.
- **Planned consequence:** Deterministic services perform enforcement; the agent cannot become the
  workflow engine.

## ADR-003 — Use Cloud Tasks, not Pub/Sub

- **Context:** The core asynchronous operation is a directed request to execute verification run
  `X`; fan-out is not required.
- **Choice:** Plan Cloud Tasks between the control plane and executor.
- **Why:** Task-level delivery, retry, rate-control, and deduplication semantics fit directed work.
- **Alternative rejected:** Pub/Sub, because an event bus is unnecessary for the current topology.
- **Planned consequence:** Revisit only if a real multi-consumer event requirement appears.

## ADR-004 — Use `.patchproof.yaml`, not LLM-generated shell commands

- **Context:** Repository code execution crosses a security boundary and must be reproducible.
- **Choice:** A validated repository execution contract will supply bounded install/test commands,
  allowed test paths, Python version, and timeout.
- **Why:** A declarative allowlisted contract is inspectable and mechanically enforceable.
- **Alternative rejected:** Letting Gemini invent commands, because prompt content must not grant
  shell authority.
- **Planned consequence:** Unsupported repositories cause validation failure or abstention rather
  than improvised execution.

## ADR-005 — Scope evidence to one claim, not PR correctness

- **Context:** Even a discriminating regression test covers only one scenario and can fail for
  irrelevant reasons.
- **Choice:** Select at most one primary claim and publish conservative scenario-scoped outcomes.
- **Why:** The claim matches what the gathered evidence can honestly support.
- **Alternative rejected:** A binary "PR verified" verdict, because it overgeneralizes evidence.
- **Planned consequence:** Mechanical status and semantic conclusion remain separate, and
  abstention is first-class.

## ADR-006 — Separate control-plane and executor credentials

- **Context:** Checked-out repository code will run in the executor.
- **Choice:** Keep GitHub write credentials and Gemini/API credentials out of the executor.
- **Why:** Least privilege reduces the value of secrets reachable by repository-controlled code.
- **Alternative rejected:** One service identity for convenience, because it broadens impact if an
  executor is compromised.
- **Planned consequence:** Services exchange bounded identifiers and results through authenticated
  infrastructure rather than shared credentials.

## ADR-007 — Use uv, a src layout, pytest, and Ruff for the foundation

- **Context:** Later phases need reproducible dependencies and tests that import the installed
  package rather than accidentally importing source from the repository root.
- **Choice:** Manage the environment and lockfile with uv, package from `src/patchproof`, test with
  pytest, and lint/format with Ruff.
- **Why:** This is a small, fast toolchain with clear responsibilities.
- **Alternative rejected:** A flat package and loosely managed virtual environment, because both
  can conceal packaging drift.
- **Consequence:** `uv.lock` is committed, tests run against the installed package, and all code and
  documentation are covered by repeatable local checks. Git normalizes repository text to LF so
  checks behave consistently across Windows and Linux environments.

## ADR-008 — Resolve full SHAs and use detached Git worktrees

- **Context:** BASE and HEAD must not move during a challenge, and the source checkout must remain
  untouched.
- **Choice:** Resolve each ref once to a full commit SHA, create separate detached worktrees, and
  verify each worktree's `HEAD` before execution.
- **Why:** Worktrees share Git objects while providing independent revision filesystems and clear
  immutable identities.
- **Alternative rejected:** Switching the source checkout between revisions, because it mutates
  developer state; two full clones, because they add avoidable object duplication and latency.
- **Consequence:** Worktree lifecycle and cleanup are deterministic responsibilities. Worktrees are
  isolation for revisions, not a malicious-code sandbox.

## ADR-009 — Parse one fixed pytest invocation through JUnit XML

- **Context:** Terminal summaries vary by verbosity, platform, and pytest version, while an LLM
  must never supply shell commands.
- **Choice:** Build a fixed subprocess argument vector for one node and normalize pytest's xUnit2
  report, retaining stdout, stderr, exit code, and duration as supporting facts.
- **Why:** Structured output distinguishes collection, assertion, runtime, skip, and xfail states
  more reliably than text matching.
- **Alternative rejected:** Parsing only stdout or accepting a model-generated command string.
- **Consequence:** Plugin-specific edge cases that cannot be normalized become invalid or
  environmental evidence. Repository command configuration remains future work.

## ADR-010 — Hash candidate bytes before and after both executions

- **Context:** Comparing a nominal filename is insufficient if bytes differ or repository code
  mutates the candidate during execution.
- **Choice:** Store immutable artifact bytes and their SHA-256, then require BASE and HEAD pre- and
  post-execution hashes plus node IDs to match.
- **Why:** Classification must be tied to the exact executed evidence artifact.
- **Alternative rejected:** Hashing only at generation time or only before execution, because
  neither detects injection drift or self-modification.
- **Consequence:** Any missing, mismatched, or changed artifact invalidates the evidence pair.

## ADR-011 — Do not infer a semantic claim outcome in Phase 1

- **Context:** Deterministic execution can observe an assertion pattern but cannot establish that
  the assertion represents the PR's stated behavior.
- **Choice:** Emit a mechanical status and pattern while leaving `claim_outcome` unset.
- **Why:** This preserves the boundary between execution facts and later bounded semantic review.
- **Alternative rejected:** Mapping BASE fail / HEAD pass directly to claim support.
- **Consequence:** `DISCRIMINATING` means the test distinguished revisions, not that PatchProof has
  verified the claim or PR.

## ADR-012 — Put local SQLite behind a behavioral store protocol

- **Context:** Phase 2 needs crash-surviving workflow state and atomic idempotency locally, while
  the target cloud architecture uses Firestore later.
- **Choice:** Define the narrow `VerificationRunStore` protocol and implement it with transactional
  SQLite for local development.
- **Why:** SQLite provides real durability, locking, indexes, and constraints with no service or
  out-of-pocket dependency. The protocol keeps SQL out of the HTTP boundary without pretending
  different database transaction models are automatically interchangeable.
- **Alternative rejected:** JSON files, because correct concurrent read-modify-write and uniqueness
  would be fragile; adding Firestore now, because it would make early tests depend on cloud setup.
- **Consequence:** Firestore must later preserve the protocol's behavioral invariants with native
  transactions and indexes. SQLite schema migrations and multi-instance deployment are not solved
  by this choice.

## ADR-013 — Use delivery identity and revision occurrence identity separately

- **Context:** GitHub may redeliver one delivery, emit multiple deliveries for the same revision,
  deliver observations out of order, or return a PR to a commit SHA seen earlier.
- **Choice:** Uniquely map every delivery ID, deduplicate an ordinary repeated BASE/HEAD occurrence,
  but allow a genuinely later return to the same SHA pair to create a new run occurrence.
- **Why:** Delivery identity prevents transport replays; SHA identity prevents duplicate work for
  the same current observation; occurrence identity preserves correct history when Git revisits an
  older commit.
- **Alternative rejected:** A global unique constraint on `(repository, PR, BASE, HEAD)`, because a
  force-push back to an earlier SHA would leave the wrong run marked current.
- **Consequence:** The local transaction uses the authenticated PR `updated_at` to distinguish a
  newer occurrence from an old replay. Equal timestamps for distinct heads remain conservatively
  first-observed because they provide no causal ordering.

## ADR-014 — Store workflow concerns as independent dimensions

- **Context:** Execution, terminal cause, evidence, revision freshness, and publication can change
  independently. Publication may need retry after execution completes, and a completed run may
  later become historical.
- **Choice:** Persist lifecycle, phase, terminal reason, evidence, claim outcome, publication state,
  and revision state separately, with deterministic transition rules and optimistic versions.
- **Why:** Each value answers one question. Retryable publication failure cannot accidentally
  restart model or execution work, and supersession does not overwrite a completed result.
- **Alternative rejected:** One large state enum encoding every combination, because it creates a
  combinatorial transition table and conflates operational failures with valid product outcomes.
- **Consequence:** More fields must be validated together. Pydantic invariants, monotonic
  transitions, SQLite transactions, and version checks enforce the allowed combinations.

## ADR-015 — Retrieve bounded context with Git and the Python AST

- **Context:** Claim selection needs changed behavior, nearby implementation, likely tests, and
  references without uploading an entire repository or trusting the current working tree.
- **Choice:** Resolve full immutable commit SHAs, read committed objects with Git, map diff hunks
  to Python AST spans, and rank bounded imports, tests, and exact references deterministically.
- **Why:** The selection is reproducible, cheap, inspectable, and aligned with the frozen Python
  scope. Explicit budgets bound files, characters, snippets, scans, and final JSON.
- **Alternative rejected:** Embeddings or broad repository ingestion, because neither has a
  demonstrated relevance advantage here and both add cost, state, and a larger disclosure area.
- **Consequence:** Dynamic references and code outside configured caps may be missed; truncation is
  recorded and the agent can abstain rather than invent missing evidence.

## ADR-016 — Use one stateless, tool-free, schema-constrained ADK agent

- **Context:** Relating PR narrative to code behavior benefits from semantic reasoning, but model
  exploration, conversational history, and shell access would weaken the trust boundary.
- **Choice:** Make one isolated Google ADK `LlmAgent` invocation using explicit
  `gemini-3.6-flash`, Pydantic input/output schemas, no tools, no prior contents, low temperature,
  and bounded output.
- **Why:** One call is easy to meter and audit. The schema allows exactly one primary claim or a
  typed abstention while deterministic services retain retrieval and enforcement.
- **Alternative rejected:** Multiple agents, free-form output, and model-driven repository tools,
  because they add coordination or authority without a requirement in claim selection.
- **Consequence:** Provider or malformed-output failures fail closed. A separate credential-gated
  test exercises the real integration; ordinary automated tests use a deterministic fake model.

## ADR-017 — Ground model claims after structured generation

- **Context:** A valid JSON schema does not prevent a model from naming a nonexistent symbol or
  citing a line range it was not shown. Repository text may also contain prompt-injection prose.
- **Choice:** Treat all PR/repository text as untrusted data, then mechanically require every
  affected symbol to equal a retrieved changed-symbol identity and every citation to be contained
  in a retrieved snippet. Require testability and a minimum confidence for selected claims.
- **Why:** Schema validation controls shape; grounding validation controls provenance. Neither
  substitutes for the other.
- **Alternative rejected:** Trusting model citations or asking for hidden chain-of-thought, because
  neither creates auditable evidence. PatchProof stores a concise reasoning summary instead.
- **Consequence:** Unsupported outputs are rejected rather than repaired in this phase, and
  `INSUFFICIENT_EVIDENCE` or `COUNTERFACTUAL_NOT_APPLICABLE` are successful semantic outcomes.

## ADR-018 — Represent repository commands as allowlisted argument arrays

- **Context:** Phase 4 introduces `.patchproof.yaml`, but parsing repository strings through a
  shell would make quoting platform-dependent and could turn model/repository text into command
  syntax.
- **Choice:** Require YAML arrays of bounded tokens and accept only predefined templates: frozen
  `uv sync` installation plus `python -m pytest` or `uv run pytest`. Invoke subprocesses with an
  argument vector and no shell.
- **Why:** The repository chooses among inspectable supported contracts; Gemini receives no command
  fields and has no way to invent a command. Python 3.12, paths, timeout, unknown keys, and file
  size are validated before use.
- **Alternative rejected:** Shell command strings and a generic executable allowlist, because both
  leave a much larger argument-level behavior surface than this phase needs.
- **Consequence:** Repositories using pip, Poetry, Hatch, tox, custom flags, or multi-step setup are
  unsupported until a concrete template is deliberately added and tested.

## ADR-019 — Validate candidate source before creating a replay artifact

- **Context:** Structured model output can still contain an unsafe path, invalid Python, the wrong
  test node, invented imports, explicit process/network behavior, or source that overwrites a
  repository file.
- **Choice:** Deterministically validate the allowed generated path, non-overwrite set, UTF-8 size,
  Python AST, exactly one declared top-level test function, grounded/standard-library imports,
  blocked modules, and selected dangerous calls. Only then construct immutable `TestArtifact`
  bytes and SHA-256 identity.
- **Why:** Model structure controls shape; AST and repository facts control executability and
  provenance. One artifact object can later be replayed unchanged on both revisions.
- **Alternative rejected:** Letting pytest be the first validator, because invalid candidates would
  consume workspaces and blur model failure with counterfactual evidence.
- **Consequence:** Static checks are intentionally conservative and incomplete. They reduce obvious
  mistakes but are not a hostile-Python sandbox; constrained execution remains a separate layer.

## ADR-020 — Enforce two candidate calls and one repair in a run-local controller

- **Context:** Unbounded generation/repair loops create cost, latency, nondeterminism, and pressure
  to manufacture a discriminating result.
- **Choice:** A run-local controller permits one initial invocation and at most one repair. It
  records attempt order, origin, parent candidate and artifact hash, bounded feedback, validation
  issues, model usage, response hash, and the validated immutable artifact.
- **Why:** The budget is mechanically testable, repair provenance is auditable, and failure to find
  evidence naturally leads to later abstention instead of another hidden retry.
- **Alternative rejected:** Agent-decided looping or automatic retries until a test passes, because
  they weaken cost and evidence integrity.
- **Consequence:** Controller state is in memory while generating, then Phase 5 maps the complete
  bounded lineage and every execution into one immutable evidence record.

## ADR-021 — Use task-specific schemas under one logical ADK agent identity

- **Context:** Claim selection and candidate generation need different structured schemas, while
  the architecture explicitly rejects a role-labelled multi-agent system.
- **Choice:** Both adapters use the same `patchproof_agent` identity, model, stateless/no-tool
  boundary, and trust policy, with a task-specific input/output schema for each isolated call.
- **Why:** ADK schemas remain narrow and directly testable without agent handoffs, delegation, shared
  chat history, or a second decision-maker.
- **Alternative rejected:** A multi-agent claim-writer/test-writer conversation and one giant union
  schema, because the former adds orchestration and the latter weakens per-call validation.
- **Consequence:** There are three thin task adapters but one logical semantic component. Common ADK
  event normalization may be consolidated later if doing so improves reliability without hiding
  the task boundaries.

## ADR-022 — Self-reject weak candidates and retain every execution

- **Context:** A valid generated test can pass or fail on both revisions and therefore provide no
  counterfactual evidence. Discarding that run would erase why repair occurred.
- **Choice:** Mechanically non-discriminating/invalid/environmental results may spend the one repair
  slot. Persist every evaluated candidate and abstain after the second weak result.
- **Why:** This makes repair explainable without creating retry-until-green pressure.
- **Alternative rejected:** Publishing any passing HEAD test or looping until BASE fails, because
  neither establishes a trustworthy differential.
- **Consequence:** Evidence documents may contain two bounded execution pairs; both remain auditable.

## ADR-023 — Let semantic assessment narrow, never override, mechanics

- **Context:** BASE failure / HEAD pass is mechanical discrimination but not proof that the
  assertion represents the selected claim.
- **Choice:** Use the same stateless, tool-free agent for one final structured relevance task, then
  deterministically restrict outcomes by the mechanical pattern and assertion relation.
- **Why:** Semantic interpretation is useful, while execution facts stay authoritative.
- **Alternative rejected:** Directly mapping every differential to support or letting the model
  rewrite statuses.
- **Consequence:** Uncertain or unrelated assertions become `INSUFFICIENT_EVIDENCE`.

## ADR-024 — Persist immutable evidence separately from workflow state

- **Context:** Publication must survive crashes and retries without repeating expensive work.
- **Choice:** Store one content-addressed JSON evidence document per run and keep lifecycle,
  outcome, and publication state in the small run record.
- **Why:** Idempotent identical writes and conflicting-write rejection create a replay boundary.
- **Alternative rejected:** Reconstructing reports from live services or storing only final prose.
- **Consequence:** A post-insert crash can reconcile terminal state from evidence without Gemini or
  pytest; schema migrations must preserve both tables.

## ADR-025 — Recover GitHub Check identity before creating on retry

- **Context:** A POST can succeed remotely while the response or local ID write is lost.
- **Choice:** Send run ID as `external_id`, persist payload hash/remote ID, and list HEAD Checks by
  name to recover an ambiguous create before another POST.
- **Why:** Ordinary idempotency state alone cannot close the remote-success/local-failure gap.
- **Alternative rejected:** Blind POST retry, because it can create duplicate Checks.
- **Consequence:** Publication retry uses only stored evidence and PATCHes a known/recovered Check.

## ADR-026 — Use short-lived GitHub App installation tokens

- **Context:** Checks write access should not use a long-lived personal token or enter executor
  persistence.
- **Choice:** Retain webhook installation ID, sign a bounded app JWT, mint a short-lived
  installation token, cache it near expiry, and give it only to the Checks client.
- **Why:** GitHub Apps provide repository-scoped identity and explicit `Checks: write` permission.
- **Alternative rejected:** Personal access tokens and persisting installation tokens.
- **Consequence:** App ID/private key remain runtime secrets; Secret Manager integration is Phase 8.

## ADR-027 — Construct a minimal child environment instead of copying the worker environment

- **Context:** Installation and pytest execute repository-controlled code. Copying `os.environ`
  would expose Gemini credentials, GitHub write credentials, webhook secrets, and unrelated host
  configuration to that code.
- **Choice:** Inherit a small named set of process/TLS variables, create isolated home/cache/temp
  paths, and add only fixed non-interactive Python/uv/pytest settings.
- **Why:** Secrets are absent by construction rather than relying on an incomplete denylist.
- **Alternative rejected:** Copying the environment and deleting known secret names, because new
  credentials could be added later and silently cross the boundary.
- **Consequence:** Some repositories that depend on ambient environment configuration are
  unsupported. This separation does not itself prevent network or filesystem access.

## ADR-028 — Bound subprocess output while draining and terminate the process tree on timeout

- **Context:** Truncating logs only after `capture_output` returns still permits unbounded worker
  memory growth, and killing only a parent can leave descendant processes running.
- **Choice:** Drain stdout/stderr concurrently into bounded prefix buffers, continue discarding
  excess bytes, start a process group, and terminate the tree on the configured timeout with a
  direct parent-kill fallback.
- **Why:** Memory retention and elapsed time become enforceable at the execution boundary, and
  noisy output cannot deadlock a child on a full pipe.
- **Alternative rejected:** Post-hoc string slicing and parent-only timeout handling.
- **Consequence:** Only output prefixes are evidence. CPU, memory, network, syscall, and filesystem
  isolation still require the Phase 8 deployment boundary.

## ADR-029 — Retry one semantic provider attempt only for explicit transient failures

- **Context:** Network, timeout, throttling, and provider-server failures can be temporary, while
  retrying malformed output or policy rejection spends cost without changing the inputs.
- **Choice:** Give each logical semantic task one initial provider attempt and at most one retry,
  restricted to explicit transient exception/status categories. Record provider-attempt count.
- **Why:** The policy tolerates a narrow class of infrastructure faults without creating an
  unbounded loop or weakening candidate/repair limits.
- **Alternative rejected:** No retries, broad exception retries, exponential retry loops, and
  “retry until valid/green.”
- **Consequence:** A second transient failure terminates the worker; future cloud orchestration may
  create a new run occurrence under a separately bounded task policy.

## ADR-030 — Persist sanitized terminal worker failures separately from evidence

- **Context:** An exception after a run enters `RUNNING` could strand it, and persisting raw
  provider or repository exceptions could disclose credentials or adversarial text.
- **Choice:** Map failures to stable codes and fixed bounded summaries, atomically transition the
  current run to terminal `FAILED`, and insert one immutable `run_failures` record. Stored evidence
  remains reserved for completed claim-scoped product outcomes.
- **Why:** Operators get durable, retry-aware failure state without treating operational failure
  as evidence or storing unsafe exception prose.
- **Alternative rejected:** Leaving failed runs active, overwriting the first failure on retry, or
  copying raw exception strings into durable state.
- **Consequence:** Detailed stack traces remain an ephemeral internal logging concern. Phase 6
  records retryability but does not automatically reschedule work.

## ADR-031 — Pin historical benchmark cases by PR identity and full BASE/HEAD SHAs

- **Context:** Branch names move, upstream test suites evolve, and an untraceable fixture cannot
  establish that an evaluation case represents a genuine historical bug fix.
- **Choice:** Version a strict manifest containing public GitHub repository/PR URLs, merged time,
  full BASE and PR HEAD SHAs, upstream test path, behavioral claim, and hashes of local reference
  and controlled artifacts. Fetch the PR ref and require its resolved commit to equal the manifest.
- **Why:** Anyone can audit the provenance and rerun the exact revision pair even after the default
  branch changes.
- **Alternative rejected:** Tags, moving branches, issue descriptions without commits, and
  hand-written cases presented as historical evidence.
- **Consequence:** A force-deleted upstream PR ref or repository outage can block a fresh clone;
  the checked-in raw report remains auditable, and repository caches allow offline reruns.

## ADR-032 — Use developer tests as hidden reference oracles, not as generated candidates

- **Context:** Historical developer regressions provide an independent behavioral oracle, but
  showing them to Gemini would leak the expected answer and invalidate generation measurement.
- **Choice:** Adapt each small developer assertion into a standalone hash-checked oracle stored
  outside the source checkout and inject it only during evaluation execution.
- **Why:** The oracle can establish whether BASE/HEAD mechanics reproduce the historical fix while
  remaining excludable from future model retrieval and prompts.
- **Alternative rejected:** Including fixed-revision test diffs in model context or claiming a
  developer oracle was generated by PatchProof.
- **Consequence:** Current 4/4 reproduction measures executor/policy capability with ideal
  artifacts, not Gemini candidate-generation quality.

## ADR-033 — Derive transparent policy metrics from complete raw rows

- **Context:** A false-support percentage can be manipulated by hiding failures, omitting negative
  cases, or changing its denominator. Zero can also mean “no false supports” or “nothing measured.”
- **Choice:** Persist every manifest scenario first, derive summaries from raw JSON, report counts
  alongside both false-support/all-support and false-support/all-negative rates, and serialize zero
  denominators as `null`. List unmeasured comparisons explicitly.
- **Why:** The result is recalculable and difficult to overstate. A reviewer can inspect every
  execution status, artifact hash, decision, output, and timing.
- **Alternative rejected:** Hand-maintained headline numbers, LLM-only judging, and silently
  dropping environmental or non-discriminating cases.
- **Consequence:** The initial humanize collection failures remained visible during development;
  the full manifest was rerun after a test-only reproducibility shim, and the final raw report
  contains the complete successful run rather than a mixture of incompatible harness versions.

## ADR-034 — Use Firestore transactions behind the existing store contract

- **Context:** Multiple scale-to-zero control instances can receive concurrent/replayed webhooks;
  a local SQLite file is neither shared nor durable across Cloud Run instances.
- **Choice:** Implement the complete `VerificationRunStore` behavior in Firestore with transaction-
  protected delivery, revision, and current-PR documents plus immutable evidence/publication data.
- **Why:** It preserves the already-tested workflow interface while making compare-and-update state
  shared and durable without operating a database server.
- **Alternative rejected:** Cloud SQL for this small deployment and non-transactional document
  writes, because the former adds cost/operations and the latter can create competing current runs.
- **Consequence:** Firestore document size/index limits apply, and production behavior still needs
  emulator/real-service integration proof beyond deterministic adapter tests.

## ADR-035 — Put only a deterministically named run identity in Cloud Tasks

- **Context:** Webhook retries must not create duplicate work, and queued payloads should not become
  another store for secrets, untrusted prose, candidate source, or commands.
- **Choice:** Name each task from the run UUID, store only `{"run_id": ...}`, authenticate it with a
  dedicated OIDC identity, and treat `AlreadyExists` as successful dispatch.
- **Why:** Firestore remains the source of truth and Cloud Tasks supplies directed delivery,
  throttling, and bounded retry without duplicating sensitive state.
- **Alternative rejected:** Pub/Sub fan-out, random task names, and full workflow snapshots in tasks.
- **Consequence:** Task-name retention limits immediate recreation of the same named task; replay
  must always be safe against the durable run record.

## ADR-036 — Separate credentialed control from a private credentialless executor

- **Context:** Repository dependencies and tests execute code outside PatchProof's authorship. A
  process that can see the GitHub App key or Gemini key would violate the credential boundary even
  if child-process environment filtering failed.
- **Choice:** Run control and executor as separate Cloud Run services and service accounts. Only
  control has Firestore/Tasks/secrets/GitHub/model authority; executor has no project role or secret
  mount and accepts only control's audience-bound identity token.
- **Why:** Credential absence is enforced by deployment identity as well as process policy.
- **Alternative rejected:** One credential-rich worker container and executor access to Firestore.
- **Consequence:** The control/executor request protocol must be hash-checked and the control task
  waits synchronously for bounded executor facts. This is least privilege, not a hostile-code
  sandbox or network-isolation claim.

## ADR-037 — Authenticate the task route in application code on public control ingress

- **Context:** GitHub cannot call an IAM-private Cloud Run service, but the same two-service control
  deployment also hosts the Cloud Tasks callback.
- **Choice:** Keep control publicly reachable for webhook HMAC verification and separately verify
  Google's task OIDC signature, audience, issuer, verified email, and exact caller account on the
  task route.
- **Why:** It avoids a third always-defined service while preventing an unauthenticated caller from
  starting a durable run by UUID.
- **Alternative rejected:** Leaving the task route public, relying only on an unverified header, or
  adding a third service before the current deployment is proven.
- **Consequence:** Token-certificate verification is application responsibility. A future dedicated
  IAM-private orchestration service is possible if operational evidence justifies it.

## ADR-038 — Execute installed contracts through the repository virtual environment path

- **Context:** The production container installs each allowed repository contract into a workspace
  `.venv`. Invoking the container interpreter after installation cannot see repository-only test
  dependencies. Resolving `.venv/bin/python` is also incorrect on Linux because that symlink may
  collapse back to the container interpreter path before process launch.
- **Choice:** When dependency installation is enabled and the validated contract starts with
  `python`, replace only that executable with the workspace `.venv/bin/python` path without
  resolving its symlink. Keep the validated remaining arguments unchanged. Disable uv's shared
  cache for the untrusted child environment and retain bounded cleanup retries for Windows file
  release latency.
- **Why:** Launching through the virtual-environment path activates Python's virtual-environment
  prefix semantics and makes the exact locked repository dependencies visible while preserving the
  contract's fixed argument array.
- **Alternative rejected:** Calling the container's global Python, resolving the virtual-environment
  symlink, or adding every supported repository's test dependencies to the PatchProof image.
- **Consequence:** Supported installed contracts currently need a Python-prefixed test command; new
  interpreter forms require explicit validation and tests rather than heuristic command rewriting.
