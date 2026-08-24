# Interview Notes

These notes describe implemented behavior through Phase 4. Future-system questions are retained
as a study checklist without invented implementation answers.

## What exists now?

PatchProof currently has three local slices. The deterministic BASE/HEAD challenge resolves full
SHAs, creates detached worktrees, executes identical hashed test bytes, parses JUnit results, and
classifies mechanical evidence. The FastAPI control plane authenticates bounded GitHub webhook
bodies, filters supported public allowlisted PR events, and persists idempotent workflow records in
SQLite with stale/supersession handling. The semantic slice retrieves bounded context, selects and
grounds one claim, and can now generate, validate, hash, and optionally repair one pytest candidate
under a strict execution contract. The same logical ADK/Gemini agent identity performs isolated
schema-specific tasks without tools or history. The slices are not yet orchestrated. PatchProof
does not yet execute generated candidates from workflow state, persist model lineage, or publish
GitHub results. The real Gemini smoke test has successfully returned a structured abstention.

## Why Python 3.12?

The project values compatibility with Google ADK/Cloud libraries, parsing libraries, pytest,
and historical Python projects more than adopting the newest interpreter. The `>=3.12,<3.13`
constraint makes an accidental 3.14 environment an explicit error instead of a silent source of
non-reproducibility.

## Why uv?

uv gives the project one tool for interpreter-aware environment creation, dependency resolution,
locking, syncing, and command execution. Committing `uv.lock` makes the resolved development
toolchain reproducible while `pyproject.toml` remains the human-edited dependency declaration.

## Why a src layout?

With a flat layout, running tests from the repository root can import an uninstalled source folder
even if packaging is broken. A src layout requires the project to be installed into the test
environment. The smoke test additionally compares the package's public version with installed
distribution metadata.

## What does the Phase 0 test prove?

It proves pytest discovers tests, the built/editable distribution exposes `patchproof`, and source
and package metadata agree on the version. It does not prove any Git, execution, evidence,
security, agent, GitHub, or cloud behavior.

## Why keep current and target architecture separate in the docs?

Architecture diagrams often become accidental product claims. Explicit labels make it possible to
discuss intended boundaries without pretending unimplemented components exist. This protects both
technical credibility and future design freedom.

## Why is BASE assertion-fail / HEAD pass not proof of PR correctness?

The pattern says one assertion in one exact test distinguished two revisions. The test may encode
the wrong requirement, cover only one narrow case, or miss regressions elsewhere. Phase 1 does not
have claim context or semantic assessment, so it emits `DISCRIMINATING` and leaves the claim
outcome unset.

## How are identical test bytes and identifiers guaranteed?

`TestArtifact` contains immutable bytes, a normalized relative path, and one node ID. The same
object is passed to both runners. SHA-256 is recorded before and after each execution and compared
with the artifact's expected hash; both results must also contain the same node ID. Any mismatch
or self-modification produces `INVALID_TEST`.

## How does PatchProof reject a test that proves nothing?

A candidate that passes on both revisions is `NON_DISCRIMINATING/BOTH_PASSED`. A candidate that
asserts unsuccessfully on both is also non-discriminating. Neither result can be elevated by a
semantic component later because mechanical evidence is authoritative.

## Why distinguish assertion failure from other failure?

Only an assertion failure is the candidate oracle evaluating to false. Import errors, collection
errors, fixture failures, arbitrary exceptions, and process failures can create a nonzero exit
without testing the behavior at all. They are normalized separately and conservatively rejected.

## Why use detached worktrees?

They materialize BASE and HEAD simultaneously without switching the developer's checkout and
without duplicating the repository's Git object database. Each input ref is resolved to a full SHA
before worktree creation and the checked-out `HEAD` is verified. Worktrees do not provide a
hostile-code sandbox.

## Why JUnit XML instead of only parsing pytest output?

JUnit exposes structured test-case outcome elements. Terminal wording changes with verbosity and
versions, so it is retained for audit but used only as a fallback. PatchProof still rejects
ambiguous plugin-specific states instead of guessing.

## What happens for skip, xfail, xpass, missing selection, and timeout?

Skip, xfail, xpass, no selected node, and multiple selected nodes are invalid candidate evidence
because the required ordinary single test did not execute as intended. Collection errors,
runtime/setup errors, process errors, and timeouts are environmental/non-comparable. None becomes
discriminating evidence.

## What does the timeout guarantee?

It bounds how long PatchProof waits for the direct pytest subprocess and records `TIMED_OUT` with
no fabricated exit code. Phase 1 does not yet guarantee termination of an entire descendant
process tree, CPU/memory quotas, or network isolation.

## Why authenticate the raw webhook body before parsing JSON?

GitHub signs the exact delivered bytes, not a parsed object. Re-serializing JSON can change key
order, whitespace, or escaping and therefore changes the message being authenticated. The control
plane streams a bounded body, computes HMAC-SHA256 with the configured shared secret, and uses a
constant-time digest comparison before parsing or persisting anything.

## How is webhook idempotency implemented?

There are two replay keys. `X-GitHub-Delivery` identifies one transport delivery and is globally
unique in the local `deliveries` table. The repository, PR number, BASE SHA, and HEAD SHA identify
the revision observation for ordinary duplicate events under different delivery IDs. Both checks
occur inside one `BEGIN IMMEDIATE` transaction, so simultaneous writers cannot both decide they
are first. This controls record creation; it is not a claim of exactly-once distributed execution.

## How do stale PR revisions work?

Every PR has at most one `CURRENT` occurrence, enforced by a partial unique SQLite index. A newer
`pull_request.updated_at` supersedes the current occurrence, preserving completed terminal reasons
or terminating active work as `SUPERSEDED`. An older out-of-order HEAD is still stored for audit
but is immediately terminal/superseded and cannot run or publish. A later return to a SHA seen
earlier creates a new occurrence rather than incorrectly reviving or hiding history.

## Why are lifecycle, terminal reason, revision state, and publication separate?

They answer different questions. A run can finish successfully (`TERMINAL/COMPLETED`), later become
historical (`revision_state=SUPERSEDED`), and already have a published result. Another completed run
can have `publication_state=RETRYABLE_FAILURE`; retrying that external write must not rerun claim
selection or tests. One giant enum would conflate these facts or need a combinatorial number of
values.

## What persists after a crash?

Once acceptance commits, the SQLite file retains immutable PR/revision identity, workflow
dimensions, audit timestamps, optimistic version, delivery mappings, and supersession links.
Uncommitted acceptance or transition work is rolled back when its connection closes. Phase 2 does
not yet persist context, generated artifacts, executor results for a run, tasks, or model usage.

## What does optimistic versioning protect?

A caller supplies the version it read. The store updates only that run and version, then increments
it. A stale caller receives `ConcurrentRunUpdateError` instead of silently overwriting a newer
state. It prevents lost updates to one record; cross-record acceptance decisions still require the
SQLite transaction and indexes.

## How can Firestore replace SQLite later?

The control plane depends on `VerificationRunStore`, not SQL. A Firestore adapter must reproduce
the behavioral contract—unique delivery resolution, one current PR occurrence, atomic
supersession, and compare-and-set versions—using Firestore transactions and suitable document IDs
or indexes. The interface is portable; SQLite's locking and partial-index mechanism is not.

## What does the Phase 2 security boundary claim?

It claims bounded body ingestion, exact-byte webhook HMAC verification, typed input validation,
public-repository enforcement, and explicit allowlisting. It does not claim GitHub App installation
authorization, rate limiting, multi-tenant isolation, a hardened database, or safe execution of
malicious repository code. The endpoint does not execute repository code yet.

## Why is an LLM needed for claim selection?

Git and the AST can identify what text and symbols changed, but they cannot reliably relate a PR's
natural-language intent to one observable precondition, action, and expected outcome. Gemini makes
that narrow semantic choice. Deterministic code still resolves revisions, retrieves and bounds
context, validates schema and provenance, and later owns execution and mechanical evidence.

## How is repository context selected without embeddings?

The retriever reads per-file diffs at full immutable SHAs, maps zero-context hunk ranges to Python
class/function/method spans, and includes bounded source around changed symbols. It ranks likely
tests using changed paths, module names, and symbol references, then ranks other exact references
and imports. Stable sorting and explicit caps make the same repository inputs produce the same
context. This can miss dynamic indirection, which is preferable to pretending exhaustive recall.

## Why not send the whole repository to Gemini?

Most files are irrelevant to one changed behavior. Whole-repository ingestion raises token cost,
noise, nondeterminism, and exposure to repository-controlled instructions. Gemini receives only
bounded PR prose plus the selected diff, symbol metadata, and snippets. It has no repository or
shell tools with which to expand that boundary.

## Why are structured output and deterministic grounding both required?

The Pydantic output schema guarantees the shape and cardinality of a response: exactly one claim
when selected, otherwise none. It cannot prove that a symbol or line range came from the supplied
repository evidence. Post-model checks therefore require exact changed-symbol membership and
snippet-contained citation ranges. An invented but well-shaped citation still fails closed.

## How is prompt injection contained during claim selection?

PR prose, diffs, comments, and source are explicitly labelled untrusted data in the system
instruction. More importantly, the agent is tool-free, stateless, and limited to one structured
response; it cannot search, run commands, or publish. Deterministic grounding blocks invented
provenance. This contains authority and output effects; it is not a claim that a model can never be
influenced by adversarial text, so abstention and rejection remain necessary.

## When does claim selection abstain?

`INSUFFICIENT_EVIDENCE` means the bounded inputs do not support a confident, testable behavioral
claim. `COUNTERFACTUAL_NOT_APPLICABLE` means BASE and HEAD are not meaningfully comparable for the
claim, for example because the relevant interface only exists in HEAD. Both are valid structured
results before test generation, not fabricated failures or reasons to weaken the confidence floor.

## Why store a reasoning summary instead of chain-of-thought?

The audit record needs a concise explanation connecting the cited context to the selected scenario,
not hidden internal deliberation. The schema therefore stores bounded `reasoning_summary` text,
along with explicit preconditions, action, expected behavior, affected symbols, citations,
confidence, testability, model/version usage, and a hash of the raw structured response.

## How is the real model integration tested and metered?

Ordinary tests inject a fake structured model and mock the ADK runner, so they are fast,
deterministic, and free. A separately marked test makes one real Gemini call only when
`PATCHPROOF_RUN_LIVE_GEMINI=1` and API-key or Vertex credentials are present. ADK event metadata is
normalized into prompt, output, total, and cached token counts when the provider supplies them.
A credentialed run completed successfully. Before it did, the live boundary exposed two schema
dialect differences: Gemini did not accept `exclusiveMinimum` or `additionalProperties` in this
response-schema path. PatchProof now emits the equivalent `minimum: 1` for positive line numbers
and removes `additionalProperties` only from the provider-facing schema; local Pydantic parsing
still forbids extra fields.

## Why does `.patchproof.yaml` use argument arrays instead of command strings?

An array such as `["python", "-m", "pytest"]` has one unambiguous executable and argument list. It
does not need shell parsing, quoting, variable expansion, pipes, redirects, or command chaining.
PatchProof additionally matches the complete array against a small set of supported templates.
The model request contains allowed generated-test paths but no installation or test commands, so
Gemini cannot acquire command authority through its structured output.

## What must pass before generated source becomes a `TestArtifact`?

The response must first satisfy the strict `CandidateTestProposal` schema. Deterministic validation
then checks that the target is a normalized new `test_*.py` path under `allowed_test_paths`, the
UTF-8 source is bounded and parses with Python's AST, exactly one declared top-level pytest test is
present, imports are standard/pytest/grounded in retrieved context, and selected process, network,
dynamic-code, and destructive calls are absent. Only then are source bytes encoded once, tied to
one node ID, and SHA-256 hashed.

## What does candidate static validation not prove?

It does not prove the test is semantically correct, deterministic, side-effect free, or safe
against adversarial Python. Python offers many indirect ways to reach behavior that a small AST
policy cannot recognize. The validator rejects obvious unsupported candidates early; constrained
execution, exact-byte replay, pytest parsing, and evidence classification remain independent
layers. PatchProof still does not claim a hardened arbitrary-code sandbox.

## How are generated imports grounded without installing or importing code?

The validator extracts import roots from the candidate AST. It accepts Python standard-library
roots and pytest, package roots derived from deterministically selected repository paths, and
third-party roots visibly imported in retrieved snippets. Relative, wildcard, explicitly blocked,
and otherwise unseen import roots are rejected. This is reproducible and avoids executing import
side effects, but it may conservatively reject a valid dependency omitted by context budgets.

## How is the candidate and repair budget enforced?

A run-local controller consumes one slot before each model invocation. It allows one initial call
and at most one repair call; a duplicate initial request, repair before an initial result, third
call, or second repair fails before Gemini is invoked. Input/output character budgets are also
checked. There is no agent-decided loop and no retry-until-green behavior.

## What candidate lineage is retained?

Every completed model call records sequence, initial/repair origin, status, structured validation
issues, bounded feedback, model usage, raw-response hash, proposal, and—when valid—the immutable
artifact. A repair points to its parent candidate ID and, if the parent was valid, its artifact
hash. This makes it possible to explain why one candidate replaced another without relying on chat
history or hidden reasoning.

## How can one logical agent have separate claim and candidate schemas?

Claim selection and test generation are separate stateless tasks under the same
`patchproof_agent` identity, Gemini model, no-tool policy, and untrusted-data boundary. Each call
uses the narrow Pydantic schema appropriate to that task. There is no claim-agent/test-agent
conversation, delegation, voting, or handoff, so this is not a multi-agent system.

## How are identical candidate bytes preserved for BASE and HEAD?

The validated source is UTF-8 encoded exactly once into the existing frozen `TestArtifact`. Its
path, node ID, bytes, and SHA-256 become the handoff object. Later execution receives that object;
it must not ask Gemini to regenerate source per revision. The Phase 1 runner verifies the hash
before and after both executions, while Phase 4 prevents an existing repository path from being
selected as the injection target.

## Future interview checklist (answers must follow implementation)

- What security does Cloud Run provide, and what does PatchProof explicitly not claim?
- How is false support measured without relying only on an LLM judge?
- Which externally visible actions are safe to retry after end-to-end orchestration exists?
- How could the design later support TypeScript without weakening the Python implementation?
