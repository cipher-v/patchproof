# Interview Notes

These notes describe implemented behavior through Phase 7. Future-system questions are retained
as a study checklist without invented implementation answers.

## What exists now?

PatchProof now connects its local slices through a durable run identity. The BASE/HEAD challenge resolves full
SHAs, creates detached worktrees, executes identical hashed test bytes, parses JUnit results, and
classifies mechanical evidence. The FastAPI control plane authenticates bounded GitHub webhook
bodies, filters supported public allowlisted PR events, and persists idempotent workflow records in
SQLite with stale/supersession handling. The semantic slice retrieves bounded context, selects and
grounds one claim, and can now generate, validate, hash, and optionally repair one pytest candidate
under a strict execution contract. The same logical ADK/Gemini agent identity performs isolated
schema-specific tasks without tools or history. The workflow executes validated installs and
candidate bytes, self-rejects weak evidence, persists complete lineage, constrains final semantic
assessment, and publishes retry-safe claim-scoped GitHub Checks. The dispatcher and store are local
boundaries pending cloud deployment. The real Gemini smoke test returned a structured abstention.

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

## Why does PatchProof self-reject a candidate that passes both revisions?

Passing HEAD proves only that a generated assertion is compatible with HEAD. If BASE also passes,
the scenario does not distinguish the change and cannot support the selected counterfactual claim.
PatchProof retains that execution, spends at most one repair, and otherwise abstains.

## Why can final semantic assessment not override mechanical evidence?

The model is useful for judging whether an assertion actually represents the grounded claim, but
it did not execute the code. Deterministic policy permits claim support only for BASE assertion
failure / HEAD pass and potential regression only for the reverse pattern. An unrelated or
uncertain assertion becomes insufficient evidence.

## What prevents publication retry from rerunning expensive work?

Execution first writes a content-addressed evidence document and terminal claim outcome.
`GitHubCheckPublisher` can access only the store and Checks client; it has no claim/candidate model,
repository, workspace, or pytest dependency. It formats every retry from the same stored JSON.

## How is an ambiguous GitHub Check create recovered?

The run UUID is the Check `external_id`. Before POSTing without a known remote ID, the client lists
same-name Checks on the immutable HEAD and searches for that external ID. A remotely successful
POST whose response was lost is therefore recovered and PATCHed rather than duplicated.

## Why is a successful GitHub Check not a whole-PR verdict?

The green result means one generated scenario provided evidence consistent with one selected
claim. The Check title, summary, conclusion, and disclaimer stay claim/scenario scoped, and include
the exact BASE, HEAD, candidate, and evidence hashes. Untested behavior remains unaddressed.

## Why is an environment allowlist stronger than deleting known secrets?

A denylist is correct only for credential names known today. A newly introduced token, cloud
credential path, or application secret would be inherited until someone remembered to update the
list. PatchProof constructs repository-child environments from a short operational allowlist, so
unknown ambient values stay outside the boundary by default. Isolated home, cache, and temporary
paths also reduce accidental reuse of user-level configuration. This is credential separation,
not a sandbox: the child still has the worker's permitted network and filesystem access.

## Why is output truncated during capture instead of after execution?

`subprocess.run(capture_output=True)` retains every byte before application code can slice the
result. A repository can therefore exhaust memory with output even if the final report is small.
PatchProof drains both pipes concurrently, retains only bounded prefixes, and discards the rest
while continuing to read. This avoids pipe deadlock and makes the retained-memory claim true at
the process boundary.

## What exactly does a process timeout stop?

The runner starts a process group and, on timeout, asks the operating system to terminate the
process tree. It then directly kills the parent if tree termination did not succeed. The result is
classified as timed out with bounded output; it is not mistaken for an assertion failure. This is
best-effort host process control, not a CPU/memory quota or proof that every external side effect
was prevented before termination.

## Which model failures are retried?

Only explicit timeouts, transport/network errors, throttling, provider server errors, and selected
deadline/conflict status codes. The identical logical request gets one retry, for a maximum of two
provider attempts. Malformed JSON, schema/grounding errors, candidate validation failures, and
ordinary client errors do not retry. Candidate repair remains an independent one-repair budget.

## How does a worker failure become durable without leaking untrusted text?

The worker maps known exception types to stable codes such as `MODEL_OUTPUT_INVALID` or
`WORKSPACE_FAILED` and fixed bounded summaries. SQLite applies terminal `FAILED` state and inserts
the first failure record in one transaction. Provider bodies, repository exception strings,
tokens, and traces are excluded from the durable/public error; internal exception chaining remains
available to controlled runtime logging.

## What happens to duplicate, stale, and superseded work?

Delivery ID and revision occurrence remain separate idempotency keys. A delivery replay does not
dispatch twice, a stale observation cannot replace the current revision, and a genuinely newer
HEAD makes an active older occurrence terminal/superseded. Both the workflow and publisher reject
superseded runs, preserving their audit history without allowing stale results to appear current.

## How are GitHub publication failures classified?

Network errors, 408/409/425/429, and 5xx responses are retryable. Other non-success responses and
a missing installation ID are terminal. Retry reloads only the immutable stored evidence and
recovers a remote Check using `external_id`; it never repeats Gemini or pytest. Error messages use
status categories and never persist GitHub response bodies or installation tokens.

## What security does Phase 6 honestly provide?

Validated argv without a shell, path and AST checks, immutable hashes, credential-minimized child
environments, bounded time/output, sanitized durable failures, and separation of model/executor/
publication authority. It does not provide hostile-code containment, outbound-network denial,
syscall filtering, read-only filesystems, CPU or memory quotas, or full supply-chain security.
Those stronger controls require the deployed executor boundary and still must be described
precisely rather than as “enterprise-grade security.”

## Why use genuine historical PRs instead of only synthetic fixtures?

Synthetic fixtures are excellent for deterministic edge cases, but their code shape and failure
mode were chosen by the implementer. Historical PRs add external provenance: an upstream author
identified a bug, changed production code, and added a regression assertion. PatchProof Bench pins
the actual BASE and PR HEAD commits and verifies the fetched PR ref, so checkout and execution are
tested against real repository structure rather than only our own examples.

## What is a hidden/reference oracle?

It is a developer-written regression assertion used to establish the expected historical
counterfactual. PatchProof stores a small adapted standalone version outside the checked-out
repository, hashes it, and injects it only for execution. A future Gemini run must exclude these
oracle files and the fixed test diff from model context. The oracle scores generation; it must not
become a hint to generation.

## How is false support measured without relying on an LLM judge?

Scenario truth comes from immutable historical provenance or controlled construction, and support
comes from deterministic policy. For the controlled no-op scenario, the same buggy revision plays
BASE and HEAD while a weak but valid candidate passes both. HEAD-only acceptance emits support even
though the fix is absent; PatchProof mechanically rejects it as non-discriminating.

The report publishes two rates. `false/support` asks what fraction of all strong supports were
false. `false/negative` asks what fraction of negative scenarios received false support. Counts and
denominators remain in raw and summary JSON. In the recorded eight-scenario policy comparison,
HEAD-only had 4/8 false supports and false-supported 4/4 negatives; PatchProof had 0/4 false strong
supports and 0/4 negatives.

## Why is the 4/4 oracle result not a Gemini benchmark result?

Because the artifacts came from developer regressions, not Gemini. The result proves that the
checkout, injection, identical-byte replay, pytest parsing, and evidence policy reproduce four real
fixes. It does not tell us how often the model selects the right claim, generates a valid test, or
abstains appropriately. The report labels live generation, semantic accuracy, token use, and model
latency as unmeasured instead of borrowing the oracle score.

## What did the humanize collection failure reveal?

`humanize` generates `_version.py` during its packaging build, so direct imports from an
uninstalled source checkout failed on both revisions. That is an environmental result, not a bug
oracle failure. The standalone artifacts now install an in-memory test-only version module before
importing production code. No upstream production file is changed. The incident illustrates why
historical benchmarks need environment normalization and why collection errors cannot count as
counterfactual evidence.

## How does PatchProof Bench prevent cherry-picking?

The strict manifest defines the complete run. `run_benchmark()` iterates every case and writes
results only after all scenarios and controlled checks complete. Raw JSON retains BASE/HEAD status,
hashes, bounded output, timing, truth, mechanics, and both strategy decisions. Summary JSON and
Markdown are regenerated from those rows rather than edited by hand. Artifact tampering fails hash
verification before any clone.

## Why does Cloud Tasks carry only a run ID?

Firestore is the authoritative record. A task needs only the stable identity required to reload it.
This avoids duplicating mutable state and prevents secrets, PR prose, candidate code, and commands
from entering the queue. The task name is derived from the UUID, so a duplicate enqueue resolves to
`AlreadyExists` and is treated as the same dispatch.

## Why does Cloud Tasks call control before executor?

The candidate does not exist at webhook time. The control task must retrieve context and run the
bounded semantic steps first. It then sends the private executor a validated contract, immutable
SHAs, and hashed candidate. Cloud Tasks still provides asynchronous webhook decoupling while the
executor remains a separate credential boundary.

## How is the public task route authenticated?

GitHub needs public control ingress, so Cloud Run IAM cannot make the whole control service private.
Cloud Tasks signs an OIDC token for a dedicated no-role account. PatchProof verifies Google's
signature and pins audience, issuer, verified email, and exact account before processing the UUID.
GitHub webhook authentication remains a separate HMAC mechanism.

## How does Firestore preserve current-run and idempotency semantics?

Delivery, revision, and current-PR identities are separate documents. Acceptance reads them inside
one transaction and writes the new run plus pointers together. Concurrent changes to the same
current pointer cause Firestore transaction retry, so a transaction reevaluates stale/supersession
logic against the winner rather than blindly creating a second current record.

## What credentials does each deployed service have?

Control can transact Firestore, enqueue Tasks, read only the three PatchProof secrets, invoke only
the executor, call Gemini, and mint GitHub App installation tokens. Executor has no project role or
secret mount. The task identity has no project role and exists only as the signed callback subject.
The Cloud Tasks service agent may mint tokens only for that task identity.

## Does using one image weaken service separation?

No. Image bytes define possible code, while the Cloud Run revision defines active role, identity,
ingress, secret mounts, environment, resources, and IAM. Executor startup selects only its app and
has no permission to resolve control secrets. Separate images could reduce code surface further,
but do not replace identity separation.

## What security does Cloud Run provide, and what does PatchProof not claim?

The deployment adds separate service identities, IAM-private executor ingress, ephemeral instance
filesystems, bounded instance/request resources, and no executor secret mounts. Existing code adds
validated argv, minimal child environments, hashes, timeouts, and bounded logs. This is constrained
execution for trusted allowlisted repositories. It is not proof of safe arbitrary malicious code:
there is no outbound-network block, syscall sandbox, or complete supply-chain containment.

## What completed Phase 8?

Cloud Build produced a pinned image digest; both Cloud Run revisions are Ready; public control and
authenticated executor liveness return 200; anonymous executor access is denied; and live
Firestore, Tasks, Secret Manager, IAM, and scaling configuration match the bounded design. GitHub
App 4711074 then delivered PR #1's signed `synchronize` event. Run
`695eaa20-7db3-492f-a57e-9819ebb54087` passed through the OIDC task route and private executor,
persisted hash-verified evidence, observed BASE assertion failure / HEAD pass, and published
successful Check 97764451438. That closes the deployment-composition proof while remaining only one
non-blind candidate-quality case.

## What did the container smoke find that native tests did not?

The first production smoke installed the fixture's locked dependencies into its workspace `.venv`
but then ran the contract with the container's global Python, which could not import the fixture's
pytest. An attempted fix that resolved `.venv/bin/python` was still wrong because the Linux symlink
resolved to the global interpreter before launch. PatchProof now invokes the un-resolved workspace
virtual-environment path, preserving Python's environment detection, and the fixture declares its
own pytest dependency. The regression test proves the selected executable is inside the repository
workspace. The container rerun then produced the expected identical-artifact BASE-fail/HEAD-pass
classification.

## Was a real historical Gemini candidate measured in the pre-cloud checkpoint?

Yes, for one bounded non-blind case. The first historical attempt on `more-itertools` PR 1223 ended
partway through its claim JSON, so strict validation failed closed before candidate generation and
did not retry the non-transient invalid response.

The truncated-output diagnosis led to low-thinking structured calls and larger hard generation
caps that remain above separate visible-response character budgets. A single post-fix attempt then
returned valid JSON and retained 1,671 total tokens, but grounding rejected the claim because the
changed 171,808-byte production file and 241,043-byte test file exceeded the old 160,000-byte scan
cap. That cap is now a still-bounded 256 KiB, and offline retrieval of the exact PR returns the
changed function, changed test class/method, and snippets.

With explicit authorization, a final normal-policy workflow selected a grounded negative-`chunked`
claim. Its initial candidate was environmental on BASE; the one permitted repair produced BASE
assertion failure / HEAD pass with identical bytes. Semantic assessment returned
`CLAIM_SUPPORTED_FOR_SCENARIO`, matching the stored developer oracle's discriminating pattern. The
four provider attempts used 12,225 total tokens with no provider retries. This proves that the real
adapters and execution path can complete this case; it does not measure blind generation because
changed test context was visible, and it does not establish an aggregate rate from one sample.

## Future interview checklist (answers must follow implementation)

- How could the design later support TypeScript without weakening the Python implementation?

## Why is the Phase 9 UI an evidence console rather than a chatbot?

The primary user task is audit, not conversation: identify the immutable run, inspect the selected
claim and exact candidate, compare BASE with HEAD, understand rejection or repair, and follow the
published Check. A chatbot would hide deterministic facts behind generated narration and create a
new prompt-injection surface. The console renders the stored typed evidence directly and keeps the
scope disclaimer visible.

## Can a public caller enumerate Firestore runs through the dashboard?

No. The API has no run-ID parameter and performs no query from request data. Deployment supplies a
bounded, unique list of featured UUIDs. For each one, the server reloads the durable run and
evidence, recomputes SHA-256, verifies repository/PR/BASE/HEAD identity, and returns a typed public
projection. A missing or inconsistent configured record produces a generic 503.

## Why is candidate source public while stdout and raw model output are not?

The exact candidate bytes are essential to understanding what was tested and are already bound to
the public artifact hash and GitHub result. Raw model responses may contain rejected or extraneous
untrusted text, and executor output can contain repository-controlled details unrelated to the
claim. The dashboard therefore publishes the validated candidate and bounded validation feedback,
but excludes raw responses, response hashes, stdout, stderr, exception details, PR body,
installation ID, and credentials.

## What do the two featured runs demonstrate?

The synchronized run demonstrates the complete positive composition: one grounded claim, one
validated candidate, identical artifact, BASE assertion failure, HEAD pass, related semantic
assessment, stored hash-verified evidence, and successful Check 97764451438. The initial run
demonstrates fail-closed behavior: two bounded candidate-construction attempts produced
environmental evidence, so PatchProof abstained and published neutral Check 97763556013. Together
they demonstrate control flow and honesty, not an aggregate success rate.

## Why is Phase 9 marked with a screenshot limitation?

The implementation, API, package, container, cloud revisions, and live HTTP behavior were tested.
The available in-app browser controller requires Node 22.22 or newer, while the installed runtime
is Node 20.17, so it could not capture a genuine rendered image. No alternate or fabricated image
was substituted. The exact four captures to take after the runtime upgrade are listed in the Phase
9 document.
