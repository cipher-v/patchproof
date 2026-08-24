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
- **Consequence:** Controller state is currently in memory and not crash-durable. Phase 5 must map
  the lineage into workflow persistence before end-to-end orchestration.

## ADR-021 — Use task-specific schemas under one logical ADK agent identity

- **Context:** Claim selection and candidate generation need different structured schemas, while
  the architecture explicitly rejects a role-labelled multi-agent system.
- **Choice:** Both adapters use the same `patchproof_agent` identity, model, stateless/no-tool
  boundary, and trust policy, with a task-specific input/output schema for each isolated call.
- **Why:** ADK schemas remain narrow and directly testable without agent handoffs, delegation, shared
  chat history, or a second decision-maker.
- **Alternative rejected:** A multi-agent claim-writer/test-writer conversation and one giant union
  schema, because the former adds orchestration and the latter weakens per-call validation.
- **Consequence:** There are two thin task adapters but one logical semantic component. Common ADK
  event normalization may be consolidated later if doing so improves reliability without hiding
  the task boundaries.
