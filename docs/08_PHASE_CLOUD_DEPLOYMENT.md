# Phase 8 - Google Cloud Deployment

## Status

**BLOCKED ON REAL GOOGLE CLOUD DEPLOYMENT PROOF.** The application, infrastructure script,
container definition, persistence adapter, queue dispatcher, private executor API, authentication
boundaries, automated tests, production image build, both service-role health checks, and a
containerized deterministic BASE/HEAD smoke are locally verified. They have not been deployed to a
Google Cloud project because this workstation has neither the Google Cloud CLI nor an authenticated
project. No cloud resource or billable API was silently enabled.

The repository is deploy-ready; it does not yet claim that a live backend exists.

## Problem and architecture

The local workflow needs durable state, directed asynchronous dispatch, public GitHub ingress, and
a separate place to execute repository code without exposing GitHub or Gemini credentials. Phase 8
maps those needs to this small Google-native architecture:

```text
GitHub webhook
      |
      v
public Cloud Run control service
HMAC validation + Firestore transaction
      |
      v
Cloud Tasks: {run_id}, deterministic task name, OIDC
      |
      v
control /tasks/verify: Google OIDC validation
      |
      +--> ADK / Gemini semantic work
      |
      +--> private Cloud Run executor (Google identity token)
                    |
                    v
          immutable BASE/HEAD challenge
                    |
                    v
Firestore evidence + control-owned GitHub Check publication
```

Cloud Tasks targets the control service's worker route rather than sending a run directly to the
executor. Claim selection and candidate construction happen before an execution request exists.
The executor is nevertheless a separate private service and owns only checkout and deterministic
test execution.

## What was implemented

### Firestore persistence

`FirestoreVerificationRunStore` implements the existing store interface. Namespaced collections
hold runs, deliveries, current PR pointers, revision pointers, immutable evidence, GitHub
publication metadata, and sanitized failures.

Acceptance and state updates use transactions. Concurrent deliveries for one PR contend on the
same current-pointer document. Delivery and revision identities remain separate deduplication
boundaries. Evidence is content-addressed and cannot be replaced with different bytes. Firestore
server clients use Application Default Credentials and IAM, not client security rules.

### Cloud Tasks dispatch and authentication

`CloudTasksRunDispatcher` creates a deterministic task name:

```text
projects/PROJECT/locations/REGION/queues/QUEUE/tasks/run-RUN_UUID_HEX
```

Its JSON body contains only `run_id`: no secret, PR prose, source, URL, or command. `AlreadyExists`
is idempotent success. The task carries a Google-signed OIDC token with an explicit audience and a
dedicated task-invoker email.

GitHub requires public control ingress, so `/tasks/verify` adds application authentication. It
validates token signature, audience, issuer, verified email, and exact service-account identity.

### Private executor

The strict executor request contains an allowlisted GitHub repository and PR number, full BASE and
HEAD SHAs, validated `ExecutionContract`, test path/node/source, and artifact SHA-256. The executor:

1. derives the public GitHub URL rather than accepting one;
2. fetches the official PR ref and BASE commit;
3. requires the fetched PR HEAD to equal the webhook SHA;
4. loads `.patchproof.yaml` from both trees and compares both with the request;
5. runs the existing bounded challenge in disposable worktrees; and
6. returns bounded facts and mechanical classification.

Artifact hashes are checked on both sides. Cloud Run IAM keeps the service private; control calls
it with a short-lived audience-bound Google identity token. Executor deployment has no Secret
Manager binding and receives no GitHub or Gemini secret.

### Real workflow composition

`CloudRunTaskProcessor` constructs the deterministic retriever, one logical ADK agent's task
adapters, evidence workflow, remote challenge, reliable worker, and GitHub App publisher. If
evidence exists already, replay goes directly to publication without Gemini or pytest.

The deployed model setting remains `gemini-3.6-flash`, the same version exercised by the successful
credential-gated live test and accepted by the 3.5-or-newer guard.

### Container and build

One pinned Python 3.12 image serves both roles. `PATCHPROOF_SERVICE_ROLE` selects `control` or
`executor`. Sharing application bytes avoids drifting builds; service identity, ingress, resources,
environment, and secret mounts remain separate. The process listens on `0.0.0.0:$PORT`.

## Least-privilege identities

| Identity | Granted by deployment | Deliberately absent |
| --- | --- | --- |
| `patchproof-control` | Firestore user, Tasks enqueuer, only three named secrets, invoke only executor, act as only task identity | owner/editor, executor identity, arbitrary secrets |
| `patchproof-executor` | no project role | Firestore, Tasks, Secret Manager, Gemini, GitHub |
| `patchproof-task-invoker` | no project role; OIDC subject only | data, execution, secrets, GitHub |
| Cloud Tasks service agent | mint token for only task identity | project-wide account administration |

The human deployer still needs permission to create these resources. Deployment permissions are
not runtime permissions.

## Secrets and cost

Secret Manager holds the webhook HMAC secret, GitHub App PEM, and Gemini API key. Only control can
read those named secrets. Input comes from local files outside the repository, not CLI literals.

The script adds no Pub/Sub, GKE, SQL, Redis, load balancer, domain, or paid observability. Both Run
services use `--min=0`. Executor concurrency/max instances are 1; control max instances are 2. The
queue allows one concurrent task and one dispatch per second, with at most three attempts. This
constrains spend but cannot guarantee a zero bill; verify credits/billing before deployment.

## Exact deployment procedure

Install Google Cloud CLI and authenticate to a project with hackathon credits or billing:

```powershell
gcloud auth login
gcloud auth application-default login
gcloud projects describe "YOUR_PROJECT_ID"
```

Create three files outside the repository, containing only their respective values:

```text
C:\secure\patchproof-webhook-secret.txt
C:\secure\patchproof-github-app.pem
C:\secure\patchproof-gemini-key.txt
```

Then run the checked-in script. PowerShell environment values do use double quotes:

```powershell
$env:PATCHPROOF_GCP_PROJECT = "YOUR_PROJECT_ID"

.\deploy\gcp\deploy.ps1 `
  -ProjectId $env:PATCHPROOF_GCP_PROJECT `
  -Region "asia-south1" `
  -AllowedRepositories "cipher-v/patchproof" `
  -GitHubAppId 123456 `
  -WebhookSecretFile "C:\secure\patchproof-webhook-secret.txt" `
  -GitHubPrivateKeyFile "C:\secure\patchproof-github-app.pem" `
  -GeminiApiKeyFile "C:\secure\patchproof-gemini-key.txt"
```

The script enables only required APIs, creates identities and bindings, creates native Firestore,
rate-limits the queue, versions secrets, builds one image, deploys private executor and public
control, authorizes control-to-executor invocation, and sets the final task audience.

Set the GitHub App webhook URL to the printed `CONTROL_URL/webhooks/github`, use the same webhook
secret, subscribe to pull-request events, grant `Checks: write`, and install only on the allowlisted
public demo repository.

## Required visible proof after deployment

```powershell
gcloud run services describe patchproof-control --region "asia-south1" --format="yaml(status.url,spec.template.spec.serviceAccountName,spec.template.metadata.annotations)"
gcloud run services describe patchproof-executor --region "asia-south1" --format="yaml(status.url,spec.template.spec.serviceAccountName,spec.template.metadata.annotations)"
gcloud tasks queues describe patchproof-verification-runs --location "asia-south1"
gcloud firestore databases describe --database="(default)"

$controlUrl = gcloud run services describe patchproof-control --region "asia-south1" --format="value(status.url)"
Invoke-RestMethod "$controlUrl/healthz"
```

Then send a genuine signed PR event and retain the run UUID, completed task, Firestore run/evidence,
both Cloud Run revisions/logs, and resulting GitHub Check. That end-to-end capture is the missing
completion proof.

## Data flow and failures

1. HMAC-verified webhook acceptance commits the immutable revision in Firestore.
2. A deterministic named task carries its run ID.
3. The task route authenticates Google OIDC before processing.
4. Control verifies the PR ref, retrieves bounded context, and runs ADK/Gemini.
5. Control calls private executor with an audience-bound token.
6. Executor independently revalidates identity, commits, contract, path, bytes, and hash.
7. Control applies evidence policy, persists evidence, and publishes from stored evidence.

Bad HMAC/repository policy fails before enqueue. Transaction conflicts get five Firestore client
attempts. Duplicate task name succeeds. Bad task OIDC returns 401. Executor IAM rejects unknown
callers before the app; contract/hash errors fail closed without internal error text. Retryable
GitHub publication returns 503 so Tasks can replay stored evidence. Durable terminal failures return
success to avoid pointless retries.

## Local container validation

The deployment deliberately uses one shared immutable image selected by
`PATCHPROOF_SERVICE_ROLE`; two Dockerfiles would create avoidable drift. The final build produced
three local tags for the same image ID:

```text
patchproof:precloud
patchproof-control:precloud
patchproof-executor:precloud
image: sha256:6853db587b33e116d079b97de5a23eb93f1810819256634eed24d9ec31294801
```

The post-fix image is 124,853,415 bytes and runs as numeric non-root user `10001`. Inspected versions were
Python 3.12.14, PatchProof 0.1.0, uv 0.12.3, Google ADK 2.7.1, Firestore 2.28.1, Cloud Tasks 2.24.0,
and Uvicorn 0.52.4.

The final image command was:

```text
docker build --file deploy/Dockerfile --tag patchproof:precloud \
  --tag patchproof-control:precloud --tag patchproof-executor:precloud .
```

The executor started through the production role selector and returned HTTP 200 / `{"status":"ok"}`.
The control code started from the same production image with the existing SQLite local factory and
also returned HTTP 200. The exact cloud-control role was not started locally because its factory
intentionally requires real Firestore Application Default Credentials, Cloud Tasks coordinates,
and a valid GitHub App key. Using fake cloud credentials merely for health would not prove that
composition; its injectable boundaries remain covered by the 14 cloud tests.

Both Uvicorn logs contained only normal startup, `0.0.0.0:8080` listening, health request, and
shutdown messages. No application error or warning appeared.

After the Gemini/context reliability changes, the shared image was rebuilt from scratch and the
executor health check was repeated successfully. Direct image inspection confirmed the deployed
code carries claim cap `8192`, thinking level `LOW`, and source cap `262144`.

### Container executor smoke

A disposable two-commit calculator Git repository reproduced the established Phase 1 fixture:
BASE subtracts, HEAD adds, and the candidate asserts `add(2, 3) == 5`. It used a real
`.patchproof.yaml`, frozen uv lock, repository dependency installation, `GitWorkspaceManager`,
`PytestRunner(install_dependencies=True)`, `BaseHeadChallenge`, artifact hashing, JUnit parsing, and
the mechanical classifier from the production image. It did not use a replacement test executor.

Observed final facts:

```text
artifact SHA-256: f25455f4eb3db8669adf0b066da0e55abaea0ac8dc4074be77676df0d543b193
BASE 1025d65635b90ca949cbdd0a55ac0d10f8208388: ASSERTION_FAILED
HEAD 493438187fdef9b983b5c3fee54c682822da5fa3: PASSED
BASE artifact unchanged: true
HEAD artifact unchanged: true
mechanical status: DISCRIMINATING
pattern: BASE_ASSERTION_FAILED_HEAD_PASSED
```

The smoke invoked the same execution core as `EphemeralChallengeExecutor`. It did not exercise the
production executor's public-GitHub PR fetch because this repository has no public PR ref and the
historical benchmark repositories intentionally do not contain `.patchproof.yaml`. Repository/PR
fetch, request validation, allowlisting, hash mismatch, and fail-closed HTTP behavior remain tested
at their automated boundaries rather than being overstated as part of this smoke.

Container validation found and fixed a real production-only bug. Resolving `.venv/bin/python`
followed its symlink back to the container interpreter, which lacks repository pytest dependencies.
The runner now preserves the repository interpreter path. Test fixtures now declare their own
pytest dependency, uv child runs use `UV_NO_CACHE=1`, and scratch/worktree removal has bounded
Windows retries. The final smoke and full suite passed after these changes.

## Real Gemini historical smoke

The initial checkpoint skipped this honestly because no credential was available. After a key was
provided outside the repository, one bounded attempt was run against
`more-itertools-1223-negative-chunk` using `gemini-3.6-flash`, the real deterministic context
retriever, actual PR narrative, and the production structured claim adapter. The provider returned
a claim-shaped response whose JSON ended prematurely (`EOF while parsing`), so schema validation
rejected it. Invalid structured output is not a transient provider failure, and the run was not
retried.

Candidate generation, BASE/HEAD candidate execution, evidence assessment, and token aggregation
were therefore not reached. The conservative result is **INVALID agent output / no claim support**,
not an abstention and not a successful candidate. The stored developer oracle for the same case
remains `BASE_ASSERTION_FAILED_HEAD_PASSED` and `DISCRIMINATING`; the live attempt did not reach the
point where it could reproduce or contradict that oracle. Usage was not retained because the
current claim-validation exception does not carry the raw response's usage metadata. This is an
observed agent-quality/reliability limitation, not a credential or historical-fixture failure.

The output policy was then corrected without weakening schema or character validation. Gemini 3.x
thinking shares the output allowance, so all three structured tasks now request low thinking and
use bounded caps sized above their existing visible-response limits: 8,192 tokens for claim
selection, 12,000 for candidate generation, and 4,096 for assessment. Invalid claim output carries
safe usage metadata and the raw-response SHA-256 into the sanitized durable SQLite/Firestore worker
failure while discarding the raw text. Regression tests pin these settings, schema migration, and
accounting behavior.

Exactly one post-fix attempt was made on the same case. It returned complete structured JSON in
4.97 seconds using 1,289 prompt, 382 output, and 1,671 total tokens in one provider attempt. Strict
grounding then rejected its selected claim because deterministic context contained zero changed
symbols and zero snippets. Candidate generation and BASE/HEAD execution again did not run, so the
result remained **INVALID / no claim support** and did not reproduce the stored oracle.

Offline diagnosis showed that the changed production and test files were 171,808 and 241,043 bytes,
both beyond the old 160,000-byte source-scan cap. The still-hard cap is now 256 KiB. A regression
test covers changed Python files above the previous boundary, and deterministic retrieval on the
exact immutable PR now returns `chunked`, `ChunkedTests`, `ChunkedTests.test_negative`, and bounded
source/test snippets. No third Gemini call was made after that fix; useful candidate generation is
therefore still unproven rather than selectively rerun until success.

A separate read-only preflight verified that the GitHub App credentials can mint an installation
token and that its single selected repository is `cipher-v/patchproof`. The selected Google Cloud
project was also confirmed active and billing-enabled. No cloud API was enabled and no resource was
created by either preflight.

## Tests and checks

Final checkpoint commands executed on 2026-08-25:

```text
uv run ruff format .
uv run ruff check .
uv lock --check
uv run pytest -q
```

- Ruff: all formatting and lint checks passed.
- Pytest: **208 passed, 1 live Gemini test skipped, 2 dependency warnings** in 116.54 seconds.
- New cloud tests: 14, all included in the passing full suite.
- Frozen dependency lock: resolved 74 packages without a lock change.
- `deploy/gcp/deploy.ps1`: PowerShell AST parse succeeded.
- `git diff --check`: passed.
- Credential/private-key/local-machine-path scans: no secret value or accidental user/workspace
  path remained; `.env.example` contains placeholders only.
- Docker Desktop 4.68.0 / Linux Engine 29.3.1 built and ran the final shared image successfully.

The two warnings are known upstream deprecations rather than blockers: Google ADK's deprecated
`BaseAgentConfig`, and FastAPI/Starlette's deprecated `httpx` TestClient compatibility layer. No
PatchProof stack trace or runtime warning appeared in the final containers.

The tests prove minimized/idempotent task creation, OIDC identity pinning, task-route authorization,
executor wire fidelity and fail-closed errors, control-to-executor token propagation, artifact
identity, and Firestore acceptance/supersession/evidence/publication/failure semantics through a
deterministic fake. They do not prove real Google IAM or deployed requests.

## Limitations and trade-offs

- No cloud resources exist yet, so visible deployment proof is blocked.
- The initial and single post-fix historical Gemini attempts both failed closed before candidate
  generation. The post-fix run retained token use and exposed a now-fixed context-size boundary,
  but historical candidate quality remains unmeasured because no third call was made.
- Local control health used SQLite mode. Firestore/Cloud Tasks/Secret Manager/IAM behavior still
  requires actual GCP deployment proof.
- Control holds one task request while calling executor; this preserves credentials but consumes a
  control request for the execution duration.
- Phase 6 makes execution failures durably terminal. Task retry mainly protects dispatch and
  stored-evidence publication; it cannot resume any arbitrary mid-workflow phase.
- Cloud Run IAM, ephemeral files, minimal identities, validated argv, bounds, and timeouts are not a
  hardened hostile-code sandbox.
- There is no VPC egress restriction, syscall sandbox, or per-process memory quota. Scope remains
  explicitly trusted, allowlisted public demo repositories.
- Large repository history can cost time/network; frozen scope expects small pure-Python projects.
- A new Cloud Run revision is needed to reliably consume a rotated `latest` secret version.

Rejected alternatives were one credential-rich executor, Pub/Sub fan-out, executor Firestore
access, arbitrary clone URLs, and model-generated commands. A third worker service could separate
task ingress later, but adds cost and deployment surface without changing the current evidence path.

## Interview questions

1. Why does a task contain only a run ID, and how is its name idempotent?
2. Why is executor private when it also validates repository and artifact identity?
3. Why must the public control task route verify OIDC in application code?
4. How do Firestore transactions preserve one current PR revision?
5. Which service identity can perform each privileged action?
6. Why can one image still provide two credential boundaries?
7. What does scale-to-zero trade for lower idle cost?
8. Why is this constrained execution, not malicious-code sandboxing?
9. What evidence is missing before claiming a real Google Cloud deployment?

## Unblocking Phase 8

Install/authenticate `gcloud`, choose the credited project, prepare the three secret files, and
authorize running `deploy/gcp/deploy.ps1`. Then capture health, IAM, queue, Firestore, signed
webhook, executor, and GitHub Check proof. Only then can Phase 8 become complete.
