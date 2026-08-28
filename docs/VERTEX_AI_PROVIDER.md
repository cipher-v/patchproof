# Gemini provider configuration

PatchProof supports two explicit Gemini provider surfaces through the same Google ADK agents:

- `GEMINI_DEVELOPER_API` uses an AI Studio / Gemini Developer API key.
- `VERTEX_AI` uses Google Cloud Application Default Credentials (ADC).

Both surfaces use the same model name, Pydantic input/output schemas, instructions, temperature,
thinking level, tool-free behavior, output budgets, validation, and bounded retry wrappers. Provider
selection changes infrastructure only. The configured production model remains
`gemini-3.6-flash`.

The ADK Gemini client is explicitly configured for one HTTP attempt. PatchProof's outer reliability
wrapper remains the sole owner of the configured transient retry, keeping provider-attempt
accounting bounded and observable.

## Local Vertex authentication

Install the Google Cloud CLI, authenticate ADC, and select the project:

```powershell
gcloud auth application-default login
gcloud config set project "patchproof-506606"

$env:PATCHPROOF_GEMINI_PROVIDER = "VERTEX_AI"
$env:GOOGLE_CLOUD_PROJECT = "patchproof-506606"
$env:GOOGLE_CLOUD_LOCATION = "global"
$env:PATCHPROOF_GEMINI_MODEL = "gemini-3.6-flash"
```

`global` is supported by the Google Gen AI SDK's Vertex path. It is an availability-oriented global
endpoint and should not be used when processing must remain in a particular region.

Run the credential-only preflight before any model request:

```powershell
uv run python -m patchproof.gemini_provider
```

The command locates ADC and prints only provider surface, project, location, and a boolean
credential-availability result. It does not refresh or print an access token, query quota, or invoke
Gemini.

Check whether the Vertex AI API is enabled without making a model request:

```powershell
gcloud services list --enabled `
  --project "patchproof-506606" `
  --filter "config.name=aiplatform.googleapis.com" `
  --format "value(config.name)"
```

If the command prints nothing, an authorized operator can enable only that API:

```powershell
gcloud services enable aiplatform.googleapis.com --project "patchproof-506606"
```

The runtime identity needs Gemini inference permission, normally supplied by
`roles/aiplatform.user`. PatchProof categorizes authentication failure, disabled API or permission
denial, unavailable model/location, invalid invocation configuration, transient throttling/server
failure, and unknown terminal provider failure without copying provider response prose or
credentials into its public exception.

## Cloud Run authentication

The control service uses its attached `patchproof-control` runtime service account through ADC.
No service-account JSON file, static access token, or Gemini API key is required. The deployment
script:

1. enables `aiplatform.googleapis.com`;
2. grants only `roles/aiplatform.user` to the control runtime identity;
3. sets `PATCHPROOF_GEMINI_PROVIDER=VERTEX_AI`, project, `global` location, and the unchanged model;
4. mounts only the GitHub webhook secret and GitHub App private key.

The private executor still has no Gemini access and receives no Gemini credential.

## Developer API compatibility

The prior path remains available explicitly for local compatibility:

```powershell
$env:PATCHPROOF_GEMINI_PROVIDER = "GEMINI_DEVELOPER_API"
$env:GEMINI_API_KEY = (Get-Content -Raw "C:\secure\gemini-api-key.txt").Trim()
$env:PATCHPROOF_GEMINI_MODEL = "gemini-3.6-flash"
```

The API key stays in the process environment and is not represented by `GeminiProviderConfig`.

## Migration smoke verification

On 2026-08-28, one non-benchmark structured claim-adapter request succeeded through Vertex AI in
project `patchproof-506606`, location `global`, using `gemini-3.6-flash`. The deterministic empty-diff
input produced a schema-valid `INSUFFICIENT_EVIDENCE` result in one provider attempt. Vertex usage
metadata reported 1,227 prompt tokens, 34 output tokens, and 1,261 total tokens. No benchmark case or
oracle material was used, and no retry was made.

## Evaluation provenance and call budgets

Sealed hard-mode V1, V2, and V3 used Gemini 3.6 Flash through the Gemini Developer API. Their
manifests, journals, results, and documentation remain unchanged. A new hard-mode manifest must
declare a provider surface, and new journals/results copy that stable value alongside the unchanged
model name.

The existing call-budget preflight remains mandatory on Vertex AI. Its operator-declared provider
capacity is an experiment assumption, not a lookup of Vertex quota. Logical model calls, retry-aware
maximum provider calls, declared capacity, and pass/fail remain separate recorded values.

## References

- [Google Gen AI SDK on Vertex AI](https://cloud.google.com/vertex-ai/generative-ai/docs/sdks/overview)
- [Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc)
- [Vertex AI generative AI access control](https://cloud.google.com/vertex-ai/generative-ai/docs/access-control)
