# Unified Cloud Analyze Product

PatchProof now uses one durable cloud run for its production CLI and Evidence Console:

```text
CLI or Evidence Console
  -> public Cloud Run control API
  -> Firestore run
  -> OIDC-authenticated Cloud Task
  -> private executor
  -> Gemini plus identical BASE/HEAD challenge
  -> hash-verified Firestore evidence
  -> the same run in terminal and Evidence Console
```

Firestore is the cloud source of truth. The browser receives only the sanitized `DashboardRun`
projection: immutable identities, selected claim, generated candidate source and lineage,
BASE/HEAD statuses, mechanical and semantic decisions, bounded failure information, and audit
hashes. It does not receive prompts, credentials, oracle data, raw environment details, or process
logs.

The final deployment uses the `patchproof-final-v1` Firestore collection namespace. The original
`patchproof` namespace contains historical and pre-final evidence. Those records are intentionally
retained for audit history, but the final deployment neither displays nor deduplicates against
them. No records are migrated or copied between namespaces.

## Current product boundary

`POST /api/analyze` accepts only canonical GitHub URLs that match the committed reproducible
historical manifests. Arbitrary public PR onboarding is not enabled. The control service derives
immutable BASE/HEAD facts from trusted metadata, creates or reuses the durable Firestore run, and
returns HTTP 202 without waiting for Gemini.

Historical third-party repositories do not need `.patchproof.yaml`. The control worker probes BASE
and HEAD independently using PatchProof's existing deterministic install templates and requires an
equivalent synthesized contract. That validated contract, its manifest origin, and the case's
repository Python paths cross the existing private executor boundary. Normal webhook runs continue
to require identical repository-owned `.patchproof.yaml` files. Neither path permits model-selected
install commands.

Only `BASE=ASSERTION_FAILED` plus `HEAD=PASSED`, followed by related semantic assessment, can support
a claim. The cloud integration does not alter prompts, model settings, repair budget, validation,
mechanical classification, or semantic policy.

## Normal production use

Set the deployed public control URL for the current PowerShell session:

```powershell
$env:PATCHPROOF_CONTROL_URL="https://patchproof-control-...run.app"
uv run patchproof analyze https://github.com/python-jsonschema/jsonschema/pull/1208
```

The CLI prints `Mode: CLOUD`, the run ID, dashboard and status URLs, durable progress changes, each
generated candidate source, BASE/HEAD results, and the terminal conclusion. A requested cloud run
never silently falls back to local execution.

Open the same evidence ledger without knowing a Python module name:

```powershell
uv run patchproof dashboard
```

Use `--no-open` to print the deployed URL without launching a browser. The dashboard automatically
discovers at most eight newest Firestore runs; no run-ID environment edit or redeployment is
required. Optional configured featured IDs are shown first and still count toward the bound.

## Local reproducibility

Without `PATCHPROOF_CONTROL_URL`, analyze retains its existing local default. Explicit commands are:

```powershell
uv run patchproof analyze --local https://github.com/python-jsonschema/jsonschema/pull/1208
uv run patchproof dashboard --local
```

`patchproof dashboard --local --no-open` serves the deterministic preview at
`http://127.0.0.1:8092/dashboard` without opening a browser.

## Public API

Create or reuse one known immutable run:

```http
POST /api/analyze
Content-Type: application/json

{"pr_url":"https://github.com/python-jsonschema/jsonschema/pull/1208"}
```

The bounded response contains `run_id`, `status`, `pr_url`, `dashboard_url`, and `result_url`.
`GET /api/runs/{run_id}` returns the same sanitized projection used by the dashboard.
`GET /dashboard/api/runs` returns the bounded newest-first snapshot. Evidence JSON is SHA-256
verified and identity-checked before projection.

## Deployment

The existing deployment script continues to reuse `patchproof-control`, `patchproof-executor`, the
`patchproof-verification-runs` queue, Firestore, and existing service accounts. Its default
allowlist includes the committed historical repositories. It copies only the two case manifests
into the image; benchmark oracle files are not copied.

```powershell
gcloud auth login
gcloud auth application-default login
gcloud config set project patchproof-506606
gcloud config get-value project

.\deploy\gcp\deploy.ps1 `
  -ProjectId "patchproof-506606" `
  -ImageTag "cloud-analyze-integration" `
  -FirestoreNamespace "patchproof-final-v1" `
  -GitHubAppId 4711074
```

`patchproof-final-v1` is also the script default, so subsequent deployments preserve the final
evidence boundary when the parameter is omitted. Namespace values use the same bounded lowercase
identifier rule as `FirestoreVerificationRunStore`. Do not point the final service back at the
historical `patchproof` namespace.

Existing enabled secret versions are reused. For an initial deployment, or an intentional rotation,
also pass `-UpdateSecretVersions`, `-WebhookSecretFile`, and `-GitHubPrivateKeyFile` with local
operator-controlled files. Never commit their contents.
After deployment, verify `/livez`, `/dashboard`, `/dashboard/api/runs`, malformed and unknown
`/api/analyze` rejection, and then make at most the separately authorized live known-PR request.

## Superseding the two legacy successful Checks

GitHub does not expose an API for deleting Check Runs. The checked-in helper is deliberately pinned
to legacy Check Runs `97764451438` and `99159359877`, their immutable heads and durable external
IDs, GitHub App `cipherv-patchproof`, and installation `156402136`. It is a dry run unless `--apply`
is supplied and never prints the PEM or minted installation token.

Review the plan without credentials or network mutation:

```powershell
uv run python scripts/supersede_legacy_checks.py
```

After explicit operator approval, use the existing local App key:

```powershell
uv run python scripts/supersede_legacy_checks.py `
  --private-key-file "C:\secure\patchproof-github-app.pem" `
  --apply
```

The guarded update changes only the two completed Checks to `neutral` with a title beginning
`Legacy PatchProof evidence — superseded` and a summary that they predate the current hardened
evidence policy. Historical Firestore records remain unchanged.
