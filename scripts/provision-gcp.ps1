[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ProjectId,

  [string]$Region = "asia-northeast1",
  [string]$RepositoryName = "mcp",
  [string]$RuntimeServiceAccountName = "steam-mcp-runner",
  [string]$WorkerServiceAccountName = "steam-mcp-worker",
  [string]$TasksServiceAccountName = "steam-mcp-tasks",
  [string]$QueueName = "steam-mcp-jobs",
  [string]$BucketName = "",
  [string]$JobCollection = "steam_jobs",
  [string]$OAuthCodeCollection = "steam_oauth_codes",
  [switch]$ConfigureSteamApiKey
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "gcloud command failed: gcloud $($Arguments -join ' ')"
  }
}

function Test-GcloudResource {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
  & gcloud @Arguments *> $null
  return $LASTEXITCODE -eq 0
}

function New-RandomHex {
  param([int]$ByteCount = 32)
  $bytes = [byte[]]::new($ByteCount)
  [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
  return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Get-PlainText {
  param([Security.SecureString]$SecureValue)
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
  }
  finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
  }
}

function Ensure-ServiceAccount {
  param([string]$Name, [string]$DisplayName)
  $email = "$Name@$ProjectId.iam.gserviceaccount.com"
  if (-not (Test-GcloudResource iam service-accounts describe $email --project $ProjectId)) {
    Invoke-Gcloud iam service-accounts create $Name `
      --project $ProjectId `
      --display-name $DisplayName
  }
  return $email
}

function Ensure-Secret {
  param([string]$Name)
  if (-not (Test-GcloudResource secrets describe $Name --project $ProjectId)) {
    Invoke-Gcloud secrets create $Name `
      --project $ProjectId `
      --replication-policy automatic
  }
}

function Get-LatestSecretVersion {
  param([string]$Name)
  $raw = @(& gcloud secrets versions list $Name `
    --project $ProjectId `
    --filter "state=ENABLED" `
    --sort-by "~createTime" `
    --limit 1 `
    --format "value(name)") -join ""
  $raw = $raw.Trim()
  if ($LASTEXITCODE -ne 0) {
    throw "Could not list Secret Manager versions for '$Name'."
  }
  if ([string]::IsNullOrWhiteSpace($raw)) {
    return $null
  }
  return ($raw -split "/")[-1]
}

function Add-SecretVersion {
  param([string]$Name, [string]$Value)
  $path = Join-Path ([IO.Path]::GetTempPath()) ("steam-mcp-secret-{0}" -f [Guid]::NewGuid().ToString("N"))
  try {
    [IO.File]::WriteAllText($path, $Value, [Text.UTF8Encoding]::new($false))
    & gcloud secrets versions add $Name `
      --project $ProjectId `
      --data-file=$path `
      --quiet
    if ($LASTEXITCODE -ne 0) {
      throw "Could not add a Secret Manager version for '$Name'."
    }
  }
  finally { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
}

function Ensure-InitialSecretValue {
  param([string]$Name, [scriptblock]$Factory)
  Ensure-Secret $Name
  if (-not (Get-LatestSecretVersion $Name)) {
    Add-SecretVersion $Name (& $Factory)
  }
}

function Grant-ProjectRole {
  param([string]$Member, [string]$Role)
  Invoke-Gcloud projects add-iam-policy-binding $ProjectId `
    --member $Member `
    --role $Role `
    --condition None `
    --quiet
}

function Grant-SecretRole {
  param([string]$Secret, [string]$ServiceAccount)
  Invoke-Gcloud secrets add-iam-policy-binding $Secret `
    --project $ProjectId `
    --member "serviceAccount:$ServiceAccount" `
    --role roles/secretmanager.secretAccessor `
    --condition None `
    --quiet
}

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  throw "Google Cloud CLI (gcloud) is not installed or is not on PATH."
}

if ([string]::IsNullOrWhiteSpace($BucketName)) {
  $BucketName = "$ProjectId-steam-mcp-jobs"
}

Write-Host "[1/8] Enabling APIs in $ProjectId..." -ForegroundColor Cyan
Invoke-Gcloud services enable `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  artifactregistry.googleapis.com `
  secretmanager.googleapis.com `
  iam.googleapis.com `
  firestore.googleapis.com `
  storage.googleapis.com `
  cloudtasks.googleapis.com `
  --project $ProjectId

Write-Host "[2/8] Creating regional Artifact Registry..." -ForegroundColor Cyan
if (-not (Test-GcloudResource artifacts repositories describe $RepositoryName --project $ProjectId --location $Region)) {
  Invoke-Gcloud artifacts repositories create $RepositoryName `
    --project $ProjectId `
    --location $Region `
    --repository-format docker `
    --description "Private MCP container images"
}
$buildIdentity = (& gcloud builds get-default-service-account --project $ProjectId).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($buildIdentity)) {
  throw "Cloud Build's default service account could not be resolved."
}
$buildIdentity = ($buildIdentity -split "/")[-1]
Invoke-Gcloud artifacts repositories add-iam-policy-binding $RepositoryName `
  --project $ProjectId `
  --location $Region `
  --member "serviceAccount:$buildIdentity" `
  --role roles/artifactregistry.writer `
  --condition None `
  --quiet

Write-Host "[3/8] Creating service identities..." -ForegroundColor Cyan
$runtimeServiceAccount = Ensure-ServiceAccount $RuntimeServiceAccountName "Steam MCP public runtime"
$workerServiceAccount = Ensure-ServiceAccount $WorkerServiceAccountName "Steam MCP private worker"
$tasksServiceAccount = Ensure-ServiceAccount $TasksServiceAccountName "Steam MCP Cloud Tasks caller"

Write-Host "[4/8] Creating secrets without rotating existing values..." -ForegroundColor Cyan
Ensure-InitialSecretValue "steam-mcp-access-token" { New-RandomHex 32 }
Ensure-InitialSecretValue "steam-mcp-worker-token" { New-RandomHex 32 }
Ensure-InitialSecretValue "steam-mcp-cursor-secret" { New-RandomHex 32 }
Ensure-InitialSecretValue "steam-mcp-oauth-login-secret" { New-RandomHex 32 }
Ensure-InitialSecretValue "steam-mcp-oauth-signing-secret" { New-RandomHex 32 }
Ensure-Secret "steam-web-api-key"

if ($ConfigureSteamApiKey) {
  $secureKey = Read-Host "Steam Web API key (empty keeps the current version)" -AsSecureString
  $plainKey = Get-PlainText $secureKey
  if (-not [string]::IsNullOrWhiteSpace($plainKey)) {
    Add-SecretVersion "steam-web-api-key" $plainKey.Trim()
  }
  $plainKey = $null
  $secureKey = $null
}

Grant-SecretRole "steam-mcp-access-token" $runtimeServiceAccount
Grant-SecretRole "steam-mcp-worker-token" $runtimeServiceAccount
Grant-SecretRole "steam-mcp-worker-token" $workerServiceAccount
Grant-SecretRole "steam-mcp-cursor-secret" $runtimeServiceAccount
Grant-SecretRole "steam-mcp-cursor-secret" $workerServiceAccount
Grant-SecretRole "steam-mcp-oauth-login-secret" $runtimeServiceAccount
Grant-SecretRole "steam-mcp-oauth-signing-secret" $runtimeServiceAccount
if (Get-LatestSecretVersion "steam-web-api-key") {
  Grant-SecretRole "steam-web-api-key" $runtimeServiceAccount
  Grant-SecretRole "steam-web-api-key" $workerServiceAccount
}

Write-Host "[5/8] Creating Firestore Native database and seven-day TTL..." -ForegroundColor Cyan
if (-not (Test-GcloudResource firestore databases describe --database "(default)" --project $ProjectId)) {
  Invoke-Gcloud firestore databases create `
    --database "(default)" `
    --location $Region `
    --type firestore-native `
    --delete-protection `
    --project $ProjectId `
    --quiet
}
$firestoreLocation = (& gcloud firestore databases describe `
  --database "(default)" `
  --project $ProjectId `
  --format "value(locationId)").Trim()
if ($LASTEXITCODE -ne 0 -or $firestoreLocation -ne $Region) {
  throw "Firestore (default) must be in '$Region'; current location is '$firestoreLocation'."
}
Invoke-Gcloud firestore fields ttls update expires_at `
  --collection-group $JobCollection `
  --database "(default)" `
  --enable-ttl `
  --project $ProjectId `
  --quiet
Invoke-Gcloud firestore fields ttls update delete_at `
  --collection-group $OAuthCodeCollection `
  --database "(default)" `
  --enable-ttl `
  --project $ProjectId `
  --quiet

Grant-ProjectRole "serviceAccount:$runtimeServiceAccount" roles/datastore.user
Grant-ProjectRole "serviceAccount:$workerServiceAccount" roles/datastore.user

Write-Host "[6/8] Creating private result bucket with a seven-day lifecycle..." -ForegroundColor Cyan
$bucketUri = "gs://$BucketName"
if (-not (Test-GcloudResource storage buckets describe $bucketUri --project $ProjectId)) {
  Invoke-Gcloud storage buckets create $bucketUri `
    --project $ProjectId `
    --location $Region `
    --uniform-bucket-level-access `
    --public-access-prevention
}
$lifecyclePath = Join-Path ([IO.Path]::GetTempPath()) "steam-mcp-lifecycle-$([Guid]::NewGuid().ToString('N')).json"
try {
  @{ rule = @(@{ action = @{ type = "Delete" }; condition = @{ age = 7 } }) } |
    ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath $lifecyclePath -Encoding utf8NoBOM
  Invoke-Gcloud storage buckets update $bucketUri `
    --project $ProjectId `
    --uniform-bucket-level-access `
    --public-access-prevention `
    --lifecycle-file $lifecyclePath
}
finally {
  if (Test-Path -LiteralPath $lifecyclePath) {
    Remove-Item -LiteralPath $lifecyclePath -Force
  }
}
Invoke-Gcloud storage buckets add-iam-policy-binding $bucketUri `
  --member "serviceAccount:$runtimeServiceAccount" `
  --role roles/storage.objectViewer `
  --quiet
Invoke-Gcloud storage buckets add-iam-policy-binding $bucketUri `
  --member "serviceAccount:$workerServiceAccount" `
  --role roles/storage.objectAdmin `
  --quiet

Write-Host "[7/8] Creating the bounded Cloud Tasks queue..." -ForegroundColor Cyan
if (-not (Test-GcloudResource tasks queues describe $QueueName --project $ProjectId --location $Region)) {
  Invoke-Gcloud tasks queues create $QueueName `
    --project $ProjectId `
    --location $Region `
    --max-dispatches-per-second 1 `
    --max-concurrent-dispatches 2 `
    --max-attempts 3
}
else {
  Invoke-Gcloud tasks queues update $QueueName `
    --project $ProjectId `
    --location $Region `
    --max-dispatches-per-second 1 `
    --max-concurrent-dispatches 2 `
    --max-attempts 3
}
Grant-ProjectRole "serviceAccount:$runtimeServiceAccount" roles/cloudtasks.enqueuer
Invoke-Gcloud iam service-accounts add-iam-policy-binding $tasksServiceAccount `
  --project $ProjectId `
  --member "serviceAccount:$runtimeServiceAccount" `
  --role roles/iam.serviceAccountUser `
  --condition None `
  --quiet

Write-Host "[8/8] Provisioning complete." -ForegroundColor Green
Write-Host "Region:            $Region"
Write-Host "Artifact Registry: $Region-docker.pkg.dev/$ProjectId/$RepositoryName"
Write-Host "Runtime identity:  $runtimeServiceAccount"
Write-Host "Worker identity:   $workerServiceAccount"
Write-Host "Tasks identity:    $tasksServiceAccount"
Write-Host "Queue:             $QueueName (rate 1/s, concurrency 2, attempts 3)"
Write-Host "Job bucket:        $bucketUri (delete after 7 days)"
Write-Host "Bearer token:      initialized only if absent; rotate only during deploy with -RotateAccessToken"
Write-Host "ChatGPT OAuth:      personal login/signing secrets initialized only if absent"
