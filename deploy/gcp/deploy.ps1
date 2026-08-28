param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "asia-south1",
    [string]$VertexLocation = "global",
    [string]$AllowedRepositories = "cipher-v/patchproof",
    [string]$ImageTag = "phase9",
    [string]$DashboardRunIds = "",
    [Parameter(Mandatory = $true)][int]$GitHubAppId,
    [Parameter(Mandatory = $true)][string]$WebhookSecretFile,
    [Parameter(Mandatory = $true)][string]$GitHubPrivateKeyFile
)

$ErrorActionPreference = "Stop"
$Queue = "patchproof-verification-runs"
$Repository = "patchproof"
$Image = "$Region-docker.pkg.dev/$ProjectId/$Repository/patchproof:$ImageTag"
$ControlService = "patchproof-control"
$ExecutorService = "patchproof-executor"
$ControlAccountName = "patchproof-control"
$ExecutorAccountName = "patchproof-executor"
$TaskAccountName = "patchproof-task-invoker"
$BuildAccountName = "patchproof-builder"
$ControlAccount = "$ControlAccountName@$ProjectId.iam.gserviceaccount.com"
$ExecutorAccount = "$ExecutorAccountName@$ProjectId.iam.gserviceaccount.com"
$TaskAccount = "$TaskAccountName@$ProjectId.iam.gserviceaccount.com"
$BuildAccount = "$BuildAccountName@$ProjectId.iam.gserviceaccount.com"

function Invoke-Gcloud {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    gcloud @Arguments
    $NativeExitCode = $LASTEXITCODE
    if ($NativeExitCode -ne 0) {
        throw "gcloud command failed with exit code $NativeExitCode."
    }
}

function Test-GcloudResource {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        # The Windows gcloud.ps1 wrapper converts expected NOT_FOUND stderr into a PowerShell
        # error record. Silence only these idempotent existence probes and inspect the native exit.
        $ErrorActionPreference = "SilentlyContinue"
        gcloud @Arguments *> $null
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

foreach ($RequiredFile in @($WebhookSecretFile, $GitHubPrivateKeyFile)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required secret file does not exist: $RequiredFile"
    }
}

Invoke-Gcloud config set project $ProjectId
Invoke-Gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com cloudtasks.googleapis.com secretmanager.googleapis.com iamcredentials.googleapis.com aiplatform.googleapis.com

$ProjectNumber = Invoke-Gcloud projects describe $ProjectId --format="value(projectNumber)"

foreach ($Account in @(
    @{ Name = $ControlAccountName; Display = "PatchProof control plane" },
    @{ Name = $ExecutorAccountName; Display = "PatchProof private executor" },
    @{ Name = $TaskAccountName; Display = "PatchProof Cloud Tasks caller" },
    @{ Name = $BuildAccountName; Display = "PatchProof image builder" }
)) {
    if (-not (Test-GcloudResource -Arguments @(
        "iam", "service-accounts", "describe", "$($Account.Name)@$ProjectId.iam.gserviceaccount.com"
    ))) {
        Invoke-Gcloud iam service-accounts create $Account.Name --display-name=$Account.Display
    }
}

foreach ($Role in @("roles/datastore.user", "roles/cloudtasks.enqueuer")) {
    Invoke-Gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:$ControlAccount" --role=$Role --condition=None
}
Invoke-Gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:$ControlAccount" --role="roles/aiplatform.user" --condition=None
Invoke-Gcloud iam service-accounts add-iam-policy-binding $TaskAccount --member="serviceAccount:$ControlAccount" --role="roles/iam.serviceAccountUser"
Invoke-Gcloud iam service-accounts add-iam-policy-binding $TaskAccount --member="serviceAccount:service-$ProjectNumber@gcp-sa-cloudtasks.iam.gserviceaccount.com" --role="roles/iam.serviceAccountTokenCreator"

foreach ($Role in @("roles/logging.logWriter", "roles/storage.objectViewer")) {
    Invoke-Gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:$BuildAccount" --role=$Role --condition=None
}

if (-not (Test-GcloudResource -Arguments @(
    "artifacts", "repositories", "describe", $Repository, "--location=$Region"
))) {
    Invoke-Gcloud artifacts repositories create $Repository --repository-format=docker --location=$Region --description="PatchProof deployment images"
}
Invoke-Gcloud artifacts repositories add-iam-policy-binding $Repository --location=$Region --member="serviceAccount:$BuildAccount" --role="roles/artifactregistry.writer"

if (-not (Test-GcloudResource -Arguments @(
    "firestore", "databases", "describe", "--database=(default)"
))) {
    Invoke-Gcloud firestore databases create --database="(default)" --location=$Region --type=firestore-native
}

if (-not (Test-GcloudResource -Arguments @(
    "tasks", "queues", "describe", $Queue, "--location=$Region"
))) {
    Invoke-Gcloud tasks queues create $Queue --location=$Region
}
Invoke-Gcloud tasks queues update $Queue --location=$Region --max-dispatches-per-second=1 --max-concurrent-dispatches=1 --max-attempts=3 --max-retry-duration=3600s

foreach ($Secret in @("patchproof-webhook-secret", "patchproof-github-private-key")) {
    if (-not (Test-GcloudResource -Arguments @("secrets", "describe", $Secret))) {
        Invoke-Gcloud secrets create $Secret --replication-policy=automatic
    }
    Invoke-Gcloud secrets add-iam-policy-binding $Secret --member="serviceAccount:$ControlAccount" --role="roles/secretmanager.secretAccessor"
}
Invoke-Gcloud secrets versions add patchproof-webhook-secret --data-file=$WebhookSecretFile
Invoke-Gcloud secrets versions add patchproof-github-private-key --data-file=$GitHubPrivateKeyFile

Invoke-Gcloud builds submit --config=deploy/cloudbuild.yaml --substitutions="_IMAGE=$Image" .

Invoke-Gcloud run deploy $ExecutorService --image=$Image --region=$Region --platform=managed --service-account=$ExecutorAccount --no-allow-unauthenticated --set-env-vars="GOOGLE_CLOUD_PROJECT=${ProjectId},PATCHPROOF_SERVICE_ROLE=executor,PATCHPROOF_ALLOWED_REPOSITORIES=${AllowedRepositories}" --startup-probe="httpGet.path=/healthz,httpGet.port=8080,timeoutSeconds=5,periodSeconds=5,failureThreshold=12" --min-instances=0 --max-instances=1 --concurrency=1 --cpu=2 --memory=2Gi --timeout=900
$ExecutorUrl = Invoke-Gcloud run services describe $ExecutorService --region=$Region --format="value(status.url)"
Invoke-Gcloud run services add-iam-policy-binding $ExecutorService --region=$Region --member="serviceAccount:$ControlAccount" --role="roles/run.invoker"

Invoke-Gcloud run deploy $ControlService --image=$Image --region=$Region --platform=managed --service-account=$ControlAccount --no-invoker-iam-check --set-env-vars="^#^GOOGLE_CLOUD_PROJECT=${ProjectId}#GOOGLE_CLOUD_LOCATION=${VertexLocation}#PATCHPROOF_GEMINI_PROVIDER=VERTEX_AI#PATCHPROOF_SERVICE_ROLE=control#PATCHPROOF_REGION=${Region}#PATCHPROOF_TASK_QUEUE=${Queue}#PATCHPROOF_CONTROL_URL=https://pending.invalid#PATCHPROOF_EXECUTOR_URL=${ExecutorUrl}#PATCHPROOF_TASK_INVOKER_EMAIL=${TaskAccount}#PATCHPROOF_ALLOWED_REPOSITORIES=${AllowedRepositories}#PATCHPROOF_GITHUB_APP_ID=${GitHubAppId}#PATCHPROOF_GEMINI_MODEL=gemini-3.6-flash#PATCHPROOF_DASHBOARD_RUN_IDS=${DashboardRunIds}" --set-secrets="PATCHPROOF_WEBHOOK_SECRET=patchproof-webhook-secret:latest,PATCHPROOF_GITHUB_PRIVATE_KEY=patchproof-github-private-key:latest" --startup-probe="httpGet.path=/healthz,httpGet.port=8080,timeoutSeconds=5,periodSeconds=5,failureThreshold=12" --min-instances=0 --max-instances=2 --concurrency=4 --cpu=1 --memory=1Gi --timeout=900
$ControlUrl = Invoke-Gcloud run services describe $ControlService --region=$Region --format="value(status.url)"
Invoke-Gcloud run services update $ControlService --region=$Region --update-env-vars="PATCHPROOF_CONTROL_URL=$ControlUrl"

Write-Output "Control URL: $ControlUrl"
Write-Output "Executor URL: $ExecutorUrl"
Write-Output "GitHub webhook URL: $ControlUrl/webhooks/github"
Write-Output "Evidence dashboard: $ControlUrl/dashboard"
Write-Output "Public health proof: Invoke-RestMethod $ControlUrl/livez"
Write-Output "Private executor proof: gcloud run services proxy $ExecutorService --region=$Region --port=8081"
