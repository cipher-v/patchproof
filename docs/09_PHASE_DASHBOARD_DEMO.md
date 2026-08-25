# Phase 9 — Evidence Dashboard and Demo Polish

**Status: IMPLEMENTATION AND LIVE DEPLOYMENT COMPLETE; SCREENSHOT CAPTURE PENDING.** The evidence
console is deployed at
<https://patchproof-control-q26kc4fdba-el.a.run.app/dashboard>. The remaining presentation task is
to capture genuine browser screenshots after the local in-app browser runtime is upgraded from
Node 20.17 to Node 22.22 or newer. No screenshot has been fabricated or claimed.

## Outcome

PatchProof now has a focused, read-only evidence console instead of a chatbot. It presents the two
real Phase 8 verification runs side by side:

- a successful claim-scoped result where the identical generated test failed its assertion on
  BASE and passed on HEAD; and
- an honest abstention where both bounded candidates failed as environmental evidence and the
  GitHub Check concluded neutral.

The view exposes the evidence necessary to audit those conclusions without exposing the webhook
payload, GitHub installation ID, model raw responses, process output, credentials, or Firestore
query capability.

## Live proof

| Item | Verified value |
| --- | --- |
| Dashboard | HTTP 200 at `/dashboard` |
| Root route | HTTP 307 to `/dashboard` |
| API | HTTP 200 at `/dashboard/api/runs` |
| Featured records | Exactly 2 explicitly configured UUIDs |
| Supported run | `695eaa20-7db3-492f-a57e-9819ebb54087` |
| Supported Check | `97764451438`, success |
| Abstention run | `2386649f-56b9-44c8-833e-ddf440a05483` |
| Abstention Check | `97763556013`, neutral |
| Evidence integrity | Both stored documents recomputed and matched SHA-256 |
| Public-field scan | 0 forbidden raw/secret field-name hits |
| Anonymous executor | HTTP 403 |
| Cloud Build | `da9d3601-74cc-44e0-9dc8-e692028c54c8`, SUCCESS |
| Image digest | `sha256:2ad74df4a335d6c4f4aa7d7aaaab8126658bd4fa0f4249278ad384d5f0838058` |
| Control revision | `patchproof-control-00009-dc6` |
| Executor revision | `patchproof-executor-00006-8x2` |

The machine-readable release record is `deploy/results/phase9-dashboard.json`.

## Information architecture

The console makes one claim-scoped run the unit of explanation. Each run shows:

1. repository, PR, immutable run UUID, BASE SHA, HEAD SHA, revision state, lifecycle, and phase;
2. selected claim, precondition, action, expected behavior, confidence, and bounded rationale;
3. every candidate attempt, source, lineage, deterministic validation issues, repair feedback, and
   artifact hash;
4. the identical-artifact BASE/HEAD execution statuses and the mechanical differential pattern;
5. semantic relevance, final claim outcome, conservative conclusion, retries, evidence hash, and
   external GitHub Check;
6. an always-visible boundary that this is evidence for one tested scenario, not PR correctness.

The success and abstention are tabs rather than aggregate cards. That keeps failure and
self-rejection inspectable instead of hiding them behind a green headline. Arrow-left and
arrow-right switch tabs for keyboard users, layout collapses for narrow screens, and reduced-motion
preferences disable nonessential transitions.

## Data and security boundary

`StoreDashboardSnapshotProvider` reads only UUIDs supplied through
`PATCHPROOF_DASHBOARD_RUN_IDS`; it does not accept a request-provided run ID and caps the list at
eight unique values. For each featured record it reloads durable evidence, recomputes its SHA-256,
and verifies repository, PR, run, BASE, and HEAD identity before returning a typed public
projection. Failure is generic HTTP 503 so a bad configured ID does not become an enumeration
oracle.

The public projection intentionally excludes pull-request body, GitHub installation ID, raw model
response text and hashes, stdout/stderr, detailed exceptions, repository-controlled error strings,
and all credentials. The browser receives a strict same-origin CSP, `DENY` frame policy, `nosniff`,
and `no-referrer`. The client assigns untrusted strings through `textContent`; it never injects
evidence through `innerHTML`. Candidate source is intentionally displayed as text because it is
part of the evidence lineage.

## Run locally

Install Python 3.12 and uv, then:

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv sync --frozen --all-groups
uv run python -m patchproof.dashboard_preview
```

Open <http://127.0.0.1:8092/dashboard>. The preview uses the checked, sanitized Phase 8 fixture in
`src/patchproof/dashboard_assets/demo.json`; it does not query cloud state or contain credentials.
Production instead uses the Firestore-backed provider.

## Reproduce verification

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build --wheel
docker build -f deploy/Dockerfile -t patchproof:phase9-smoke .
```

The Phase 9 checkpoint passed 216 tests with one explicitly credential-gated/billable Gemini test
skipped. The production wheel contained the dashboard modules and four static assets. A container
smoke returned HTTP 200 for the dashboard and API, served two runs, and emitted the strict CSP.

Deployment is reproducible through `deploy/gcp/deploy.ps1`. Pass the two featured UUIDs through
`-DashboardRunIds`; pass secret **file paths**, never secret values. `gcloud` authentication, the
project, billing, enabled APIs, GitHub App, repository installation, and three local secret files
remain prerequisites described in `docs/08_PHASE_CLOUD_DEPLOYMENT.md`.

## Four-minute demo script

**0:00–0:35 — Problem.** Open PR #1. Explain that “tests passed on HEAD” is weak evidence because a
test can be irrelevant or already pass before the patch. PatchProof asks a narrower question: can
one generated scenario distinguish immutable BASE from HEAD and does that assertion represent one
grounded claim?

**0:35–1:05 — Architecture.** Show the architecture diagram. A signed GitHub webhook creates one
durable Firestore run, Cloud Tasks calls the authenticated control route, the single bounded Gemini
agent selects a claim and proposes a test, and the credentialless private executor challenges the
same hashed bytes on BASE and HEAD. Control stores evidence and publishes a GitHub Check.

**1:05–2:15 — Successful evidence.** Open the dashboard's “Supported” tab. Point out run UUID,
immutable SHAs, selected claim, exact generated source, artifact SHA, and one candidate. Scroll to
BASE `ASSERTION_FAILED` and HEAD `PASSED`, then show `DISCRIMINATING`, semantic relation `RELATED`,
the scoped conclusion, recomputed evidence hash, and Check 97764451438. Say explicitly that the
green Check supports only this claim in this scenario.

**2:15–3:05 — Failure and self-rejection.** Switch to “Abstained.” Show two candidate attempts and
their parent/repair lineage. Both executions ended as environmental/test errors, so deterministic
policy refused to convert them into support. The run still reached a durable terminal state and
published neutral Check 97763556013. This is the fail-closed behavior, not a hidden failed demo.

**3:05–3:35 — Evaluation.** Open the benchmark summary. Four developer-written historical
regression oracles reproduced BASE assertion failure / HEAD pass. Four controlled weak candidates
passed both sides and were rejected. HEAD-only acceptance would have falsely supported all four
negative controls; PatchProof supported none. Clarify that this measures the executor/evidence
policy, not a 4/4 Gemini generation rate.

**3:35–4:00 — Boundaries and close.** Show the IAM-private executor returning 403 anonymously and
the dashboard scope notice. State current limits: public allowlisted Python/pytest repositories,
no hostile-code sandbox, only a small benchmark and two live UI examples, no aggregate candidate
quality estimate. End with: “PatchProof does not tell you the PR is correct. It shows exactly what
was tested, against which revisions, and why the evidence was accepted or rejected.”

## Devpost-ready description

### Short description

PatchProof is a GitHub agent that turns one grounded pull-request claim into executable,
counterfactual evidence. It generates a bounded pytest scenario, runs the identical hashed test on
immutable BASE and HEAD revisions, rejects non-discriminating or unrelated results, and publishes
an auditable, claim-scoped GitHub Check.

### Full description

Review tools often summarize a diff or run tests only on the proposed revision. Neither answers
the counterfactual: did this patch cause the claimed behavior to change? PatchProof receives a
signed GitHub pull-request event, retrieves bounded diff and symbol context, and uses one stateless,
tool-free Google ADK agent with Gemini 3.6 Flash to select at most one testable behavioral claim and
propose a structured pytest candidate. Deterministic code—not the model—validates paths, syntax,
imports, calls, execution contracts, budgets, and artifact identity.

An IAM-private, credentialless Cloud Run executor checks out full immutable BASE and HEAD SHAs and
runs the exact same test bytes against both. Mechanical policy rejects candidates that pass both,
fail environmentally, mutate, or otherwise do not discriminate. A bounded semantic assessment
can confirm relevance but cannot override the execution facts. Content-addressed evidence is
stored in Firestore and published through a retry-safe GitHub App Check. Insufficient evidence is
a valid neutral outcome.

The live console exposes both a successful BASE-assertion-failed/HEAD-passed run and the first
delivery's bounded abstention. PatchProof Bench also replays four genuine historical developer
regression oracles plus four controlled weak negatives, keeping policy results separate from
unmeasured model-generation quality. PatchProof deliberately does not claim whole-PR correctness
or safe execution of arbitrary hostile repositories.

### Suggested submission facts

- Built with Python 3.12, FastAPI, Google ADK, Gemini 3.6 Flash, Cloud Run, Cloud Tasks, Firestore,
  Secret Manager, Artifact Registry, Cloud Build, GitHub Apps, pytest, uv, and Ruff.
- Live dashboard: <https://patchproof-control-q26kc4fdba-el.a.run.app/dashboard>
- Source: <https://github.com/cipher-v/patchproof>
- Demonstrated GitHub PR: <https://github.com/cipher-v/patchproof/pull/1>
- Do not claim a 100% agent success rate, general repository support, whole-PR verification, or a
  hardened sandbox.

## Screenshot plan and current blocker

Required genuine captures:

1. desktop supported-run overview with claim, BASE/HEAD, and conclusion visible;
2. desktop abstention view with both rejected candidates and repair lineage visible;
3. narrow/mobile view showing responsive stacking;
4. GitHub Check success and neutral results on PR #1.

The in-app browser control available during Phase 9 refused to start under installed Node 20.17;
it requires Node 22.22 or newer. Product HTTP, package, API, and container verification proceeded,
but no alternate screenshot mechanism was used and no screenshot file is checked in. After Node is
upgraded, capture the four real views and add them under `docs/screenshots/`, then replace this
paragraph with exact filenames and capture date.

## Final checklist

- [x] Focused evidence dashboard, not a chatbot
- [x] PR/run identity, lifecycle, phase, revisions, and final GitHub result
- [x] Selected claim and complete candidate source/lineage
- [x] BASE/HEAD mechanical evidence and semantic boundary
- [x] Rejected candidates, repair, retries, and abstention visible
- [x] Explicit allowlist and sanitized public projection
- [x] Responsive and keyboard-aware client with strict browser headers
- [x] Local fixture, unit/API tests, wheel verification, and container smoke
- [x] Live Cloud Run deployment and Firestore-backed API verification
- [x] README, architecture, ADR, interview notes, Devpost copy, and four-minute script
- [ ] Genuine screenshots (blocked only by local browser-control Node version)

## Honest limitations

- PatchProof supports explicitly allowlisted public Python/pytest repositories with compatible
  deterministic contracts; it is not language- or repository-general.
- The executor has separate identity, no secrets, bounded commands/time/output, and an ephemeral
  filesystem, but no outbound-network denial, syscall sandbox, or proof against malicious code.
- Four historical developer oracles measure execution and evidence policy. They do not measure
  live candidate generation. The two featured live runs are demonstrations, not a quality rate.
- The successful live case is non-blind because changed test context was visible.
- Static validation and semantic assessment can still reject good tests or miss subtle bad ones.
- Adding a run UUID to the public dashboard is an explicit publication decision because candidate
  source and selected claim are displayed.
- Screenshots remain pending for the toolchain reason documented above.
