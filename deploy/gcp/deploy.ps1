param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "asia-south1",
    [string]$AllowedRepositories = "cipher-v/patchproof",
    [Parameter(Mandatory = $true)][int]$GitHubAppId,
    [Parameter(Mandatory = $true)][string]$WebhookSecretFile,
    [Parameter(Mandatory = $true)][string]$GitHubPrivateKeyFile,
    [Parameter(Mandatory = $true)][string]$GeminiApiKeyFile
)

$ErrorActionPreference = "Stop"
$Queue = "patchproof-verification-runs"
$Repository = "patchproof"
$Image = "$Region-docker.pkg.dev/$ProjectId/$Repository/patchproof:phase8"
$ControlService = "patchproof-control"
$ExecutorService = "patchproof-executor"
$ControlAccountName = "patchproof-control"
$ExecutorAccountName = "patchproof-executor"
$TaskAccountName = "patchproof-task-invoker"
$ControlAccount = "$ControlAccountName@$ProjectId.iam.gserviceaccount.com"
$ExecutorAccount = "$ExecutorAccountName@$ProjectId.iam.gserviceaccount.com"
$TaskAccount = "$TaskAccountName@$ProjectId.iam.gserviceaccount.com"

foreach ($RequiredFile in @($WebhookSecretFile, $GitHubPrivateKeyFile, $GeminiApiKeyFile)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required secret file does not exist: $RequiredFile"
    }
}

gcloud config set project $ProjectId
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com cloudtasks.googleapis.com secretmanager.googleapis.com iamcredentials.googleapis.com

$ProjectNumber = gcloud projects describe $ProjectId --format="value(projectNumber)"

foreach ($Account in @(
    @{ Name = $ControlAccountName; Display = "PatchProof control plane" },
    @{ Name = $ExecutorAccountName; Display = "PatchProof private executor" },
    @{ Name = $TaskAccountName; Display = "PatchProof Cloud Tasks caller" }
)) {
    gcloud iam service-accounts describe "$($Account.Name)@$ProjectId.iam.gserviceaccount.com" 2>$null
    if ($LASTEXITCODE -ne 0) {
        gcloud iam service-accounts create $Account.Name --display-name=$Account.Display
    }
}

foreach ($Role in @("roles/datastore.user", "roles/cloudtasks.enqueuer")) {
    gcloud projects add-iam-policy-binding $ProjectId --member="serviceAccount:$ControlAccount" --role=$Role --condition=None
}
gcloud iam service-accounts add-iam-policy-binding $TaskAccount --member="serviceAccount:$ControlAccount" --role="roles/iam.serviceAccountUser"
gcloud iam service-accounts add-iam-policy-binding $TaskAccount --member="serviceAccount:service-$ProjectNumber@gcp-sa-cloudtasks.iam.gserviceaccount.com" --role="roles/iam.serviceAccountTokenCreator"

gcloud artifacts repositories describe $Repository --location=$Region 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $Repository --repository-format=docker --location=$Region --description="PatchProof deployment images"
}

gcloud firestore databases describe --database="(default)" 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud firestore databases create --database="(default)" --location=$Region --type=firestore-native
}

gcloud tasks queues describe $Queue --location=$Region 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud tasks queues create $Queue --location=$Region
}
gcloud tasks queues update $Queue --location=$Region --max-dispatches-per-second=1 --max-concurrent-dispatches=1 --max-attempts=3 --max-retry-duration=3600s

foreach ($Secret in @("patchproof-webhook-secret", "patchproof-github-private-key", "patchproof-gemini-api-key")) {
    gcloud secrets describe $Secret 2>$null
    if ($LASTEXITCODE -ne 0) {
        gcloud secrets create $Secret --replication-policy=automatic
    }
    gcloud secrets add-iam-policy-binding $Secret --member="serviceAccount:$ControlAccount" --role="roles/secretmanager.secretAccessor"
}
gcloud secrets versions add patchproof-webhook-secret --data-file=$WebhookSecretFile
gcloud secrets versions add patchproof-github-private-key --data-file=$GitHubPrivateKeyFile
gcloud secrets versions add patchproof-gemini-api-key --data-file=$GeminiApiKeyFile

gcloud builds submit --config=deploy/cloudbuild.yaml --substitutions="_IMAGE=$Image" .

gcloud run deploy $ExecutorService --image=$Image --region=$Region --platform=managed --service-account=$ExecutorAccount --no-allow-unauthenticated --set-env-vars="^:^GOOGLE_CLOUD_PROJECT=$ProjectId:PATCHPROOF_SERVICE_ROLE=executor:PATCHPROOF_ALLOWED_REPOSITORIES=$AllowedRepositories" --min=0 --max=1 --concurrency=1 --cpu=2 --memory=2Gi --timeout=900
$ExecutorUrl = gcloud run services describe $ExecutorService --region=$Region --format="value(status.url)"
gcloud run services add-iam-policy-binding $ExecutorService --region=$Region --member="serviceAccount:$ControlAccount" --role="roles/run.invoker"

gcloud run deploy $ControlService --image=$Image --region=$Region --platform=managed --service-account=$ControlAccount --allow-unauthenticated --set-env-vars="^:^GOOGLE_CLOUD_PROJECT=$ProjectId:PATCHPROOF_SERVICE_ROLE=control:PATCHPROOF_REGION=$Region:PATCHPROOF_TASK_QUEUE=$Queue:PATCHPROOF_CONTROL_URL=https://pending.invalid:PATCHPROOF_EXECUTOR_URL=$ExecutorUrl:PATCHPROOF_TASK_INVOKER_EMAIL=$TaskAccount:PATCHPROOF_ALLOWED_REPOSITORIES=$AllowedRepositories:PATCHPROOF_GITHUB_APP_ID=$GitHubAppId:PATCHPROOF_GEMINI_MODEL=gemini-3.6-flash:GOOGLE_GENAI_USE_VERTEXAI=false" --set-secrets="PATCHPROOF_WEBHOOK_SECRET=patchproof-webhook-secret:latest,PATCHPROOF_GITHUB_PRIVATE_KEY=patchproof-github-private-key:latest,GOOGLE_API_KEY=patchproof-gemini-api-key:latest" --min=0 --max=2 --concurrency=4 --cpu=1 --memory=1Gi --timeout=900
$ControlUrl = gcloud run services describe $ControlService --region=$Region --format="value(status.url)"
gcloud run services update $ControlService --region=$Region --update-env-vars="PATCHPROOF_CONTROL_URL=$ControlUrl"

Write-Output "Control URL: $ControlUrl"
Write-Output "Executor URL: $ExecutorUrl"
Write-Output "GitHub webhook URL: $ControlUrl/webhooks/github"
Write-Output "Public health proof: Invoke-RestMethod $ControlUrl/healthz"
Write-Output "Private executor proof: gcloud run services proxy $ExecutorService --region=$Region --port=8081"
