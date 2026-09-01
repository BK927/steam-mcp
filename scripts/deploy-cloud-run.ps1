[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ProjectId,
  [string]$Region = "asia-northeast1",
  [string]$ServiceName = "steam-mcp",
  [string]$WorkerServiceName = "steam-mcp-worker",
  [string]$RepositoryName = "mcp",
  [string]$ImageName = "steam-mcp",
  [string]$RuntimeServiceAccountName = "steam-mcp-runner",
  [string]$WorkerServiceAccountName = "steam-mcp-worker",
  [string]$TasksServiceAccountName = "steam-mcp-tasks",
  [string]$QueueName = "steam-mcp-jobs",
  [string]$BucketName = "",
  [string]$JobCollection = "steam_jobs",
  [int]$SmokeAppId = 570,
  [string]$TokenEnvironmentVariable = "STEAM_MCP_ACCESS_TOKEN",
  [switch]$RotateAccessToken,
  [switch]$Promote
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) { throw "gcloud command failed: gcloud $($Arguments -join ' ')" }
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

function Get-LatestSecretVersion {
  param([string]$Name, [bool]$Required = $true)
  if (-not (Test-GcloudResource secrets describe $Name --project $ProjectId)) {
    if ($Required) { throw "Missing secret '$Name'. Run provision-gcp.ps1 first." }
    return $null
  }
  $raw = (& gcloud secrets versions list $Name --project $ProjectId --filter "state=ENABLED" --sort-by "~createTime" --limit 1 --format "value(name)").Trim()
  if ($LASTEXITCODE -ne 0) { throw "Could not list versions for '$Name'." }
  if ([string]::IsNullOrWhiteSpace($raw)) {
    if ($Required) { throw "Secret '$Name' has no enabled numeric version." }
    return $null
  }
  return ($raw -split "/")[-1]
}

function Add-SecretVersion {
  param([string]$Name, [string]$Value)
  $Value | & gcloud secrets versions add $Name --project $ProjectId --data-file=- --quiet
  if ($LASTEXITCODE -ne 0) { throw "Could not rotate '$Name'." }
}

function Get-ServiceDocument {
  param([string]$Name)
  $json = & gcloud run services describe $Name --project $ProjectId --region $Region --format json
  if ($LASTEXITCODE -ne 0) { throw "Cloud Run service '$Name' could not be described." }
  return ($json | ConvertFrom-Json)
}

function Get-TaggedUrl {
  param([object]$Document, [string]$Tag)
  $entries = @()
  if ($Document.status.PSObject.Properties.Name -contains "traffic") { $entries += @($Document.status.traffic) }
  if ($Document.status.PSObject.Properties.Name -contains "trafficStatuses") { $entries += @($Document.status.trafficStatuses) }
  $match = @($entries | Where-Object { $_.tag -eq $Tag -and $_.url } | Select-Object -First 1)
  if ($match.Count -eq 0) { return $null }
  return [string]$match[0].url
}

function Get-TaggedRevision {
  param([object]$Document, [string]$Tag)
  $entries = @()
  if ($Document.status.PSObject.Properties.Name -contains "traffic") { $entries += @($Document.status.traffic) }
  if ($Document.status.PSObject.Properties.Name -contains "trafficStatuses") { $entries += @($Document.status.trafficStatuses) }
  $match = @($entries | Where-Object { $_.tag -eq $Tag -and $_.revisionName } | Select-Object -First 1)
  if ($match.Count -eq 0) { return $null }
  return [string]$match[0].revisionName
}

function Deploy-WorkerCandidate {
  param([string]$RevisionSuffix, [string]$WorkerEndpoint, [bool]$NoTraffic)
  $envValues = "^@^MCP_TRANSPORT=http@HOST=0.0.0.0@PORT=8080@HEALTH_PATH=/healthz@STEAM_PROCESS_ROLE=worker@STEAM_JOB_BACKEND=gcp@GCP_PROJECT=$ProjectId@GCP_LOCATION=$Region@STEAM_JOB_BUCKET=$BucketName@STEAM_JOB_COLLECTION=$JobCollection@STEAM_JOB_TTL_SECONDS=604800"
  $secretValues = "^@^STEAM_JOB_WORKER_TOKEN=steam-mcp-worker-token:$workerTokenVersion@STEAM_CURSOR_SECRET=steam-mcp-cursor-secret:$cursorSecretVersion"
  if ($steamApiVersion) { $secretValues += "@STEAM_API_KEY=steam-web-api-key:$steamApiVersion" }
  $arguments = @(
    "run", "deploy", $WorkerServiceName, "--project", $ProjectId, "--region", $Region,
    "--image", $immutableImage, "--no-allow-unauthenticated", "--ingress", "all",
    "--execution-environment", "gen2", "--service-account", $workerServiceAccount,
    "--port", "8080", "--cpu", "1", "--memory", "1Gi", "--concurrency", "1",
    "--timeout", "1800", "--min-instances", "0", "--max-instances", "2",
    "--set-env-vars", $envValues, "--set-secrets", $secretValues,
    "--revision-suffix", $RevisionSuffix, "--tag", "candidate",
    "--labels", "app=steam-mcp-worker,git-sha=$shortSha", "--quiet"
  )
  if ($WorkerEndpoint) { $arguments += @("--add-custom-audiences", $WorkerEndpoint) }
  if ($NoTraffic) { $arguments += "--no-traffic" }
  Invoke-Gcloud @arguments
}

function Deploy-McpCandidate {
  param([string]$RevisionSuffix, [string]$PublicBaseUrl, [string]$AllowedHosts, [string]$WorkerEndpoint, [bool]$NoTraffic)
  $envValues = "^@^MCP_TRANSPORT=http@HOST=0.0.0.0@PORT=8080@MCP_PATH=/mcp@HEALTH_PATH=/healthz@HTTP_MAX_BODY_BYTES=2097152@MCP_ALLOW_UNAUTHENTICATED=false@PUBLIC_BASE_URL=$PublicBaseUrl@MCP_ALLOWED_HOSTS=$AllowedHosts@STEAM_PROCESS_ROLE=mcp@STEAM_JOB_BACKEND=gcp@GCP_PROJECT=$ProjectId@GCP_LOCATION=$Region@STEAM_JOB_QUEUE=$QueueName@STEAM_JOB_WORKER_URL=$WorkerEndpoint@STEAM_JOB_WORKER_SERVICE_ACCOUNT=$tasksServiceAccount@STEAM_JOB_BUCKET=$BucketName@STEAM_JOB_COLLECTION=$JobCollection@STEAM_JOB_TTL_SECONDS=604800@STEAM_CURSOR_TTL_SECONDS=86400@STEAM_MAX_RESULT_BYTES=12288"
  $secretValues = "^@^MCP_ACCESS_TOKEN=steam-mcp-access-token:$accessVersion@STEAM_JOB_WORKER_TOKEN=steam-mcp-worker-token:$workerTokenVersion@STEAM_CURSOR_SECRET=steam-mcp-cursor-secret:$cursorSecretVersion"
  if ($steamApiVersion) { $secretValues += "@STEAM_API_KEY=steam-web-api-key:$steamApiVersion" }
  $arguments = @(
    "run", "deploy", $ServiceName, "--project", $ProjectId, "--region", $Region,
    "--image", $immutableImage, "--allow-unauthenticated", "--ingress", "all",
    "--execution-environment", "gen2", "--service-account", $runtimeServiceAccount,
    "--port", "8080", "--cpu", "1", "--memory", "512Mi", "--concurrency", "10",
    "--timeout", "300", "--min-instances", "0", "--max-instances", "1",
    "--set-env-vars", $envValues, "--set-secrets", $secretValues,
    "--revision-suffix", $RevisionSuffix, "--tag", "candidate",
    "--labels", "app=steam-mcp,git-sha=$shortSha", "--quiet"
  )
  if ($NoTraffic) { $arguments += "--no-traffic" }
  Invoke-Gcloud @arguments
}

function Test-HttpStatus {
  param([string]$Url, [int]$ExpectedStatus)
  try {
    $response = Invoke-WebRequest -Uri $Url -Method Post -ContentType "application/json" -Body "{}"
    $status = [int]$response.StatusCode
  }
  catch {
    if (-not $_.Exception.Response) { throw }
    $status = [int]$_.Exception.Response.StatusCode
  }
  if ($status -ne $ExpectedStatus) { throw "Expected HTTP $ExpectedStatus from '$Url', received $status." }
}

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) { throw "Google Cloud CLI (gcloud) is not installed or is not on PATH." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git is required for SHA-tagged builds." }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv is required for the pinned MCP SDK smoke client." }
if ([string]::IsNullOrWhiteSpace($BucketName)) { $BucketName = "$ProjectId-steam-mcp-jobs" }

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gitSha = (& git -C $projectRoot rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $gitSha -notmatch "^[0-9a-f]{40}$") { throw "The repository HEAD is not a full Git SHA." }
$dirty = & git -C $projectRoot status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) { throw "Deployment requires a clean worktree so the SHA identifies the exact build context." }

$shortSha = $gitSha.Substring(0, 12)
$revisionNonce = Get-Date -AsUTC -Format "yyyyMMddHHmmss"
$runtimeServiceAccount = "$RuntimeServiceAccountName@$ProjectId.iam.gserviceaccount.com"
$workerServiceAccount = "$WorkerServiceAccountName@$ProjectId.iam.gserviceaccount.com"
$tasksServiceAccount = "$TasksServiceAccountName@$ProjectId.iam.gserviceaccount.com"
$taggedImage = "$Region-docker.pkg.dev/$ProjectId/$RepositoryName/${ImageName}:$gitSha"

if ($RotateAccessToken) { Add-SecretVersion "steam-mcp-access-token" (New-RandomHex 32) }
$accessVersion = Get-LatestSecretVersion "steam-mcp-access-token"
$workerTokenVersion = Get-LatestSecretVersion "steam-mcp-worker-token"
$cursorSecretVersion = Get-LatestSecretVersion "steam-mcp-cursor-secret"
$steamApiVersion = Get-LatestSecretVersion "steam-web-api-key" $false

Write-Host "[1/7] Building immutable image $taggedImage..." -ForegroundColor Cyan
Push-Location $projectRoot
try { Invoke-Gcloud builds submit . --project $ProjectId --tag $taggedImage --quiet }
finally { Pop-Location }
$digest = (& gcloud artifacts docker images describe $taggedImage --project $ProjectId --format "value(image_summary.digest)").Trim()
if ($LASTEXITCODE -ne 0 -or $digest -notmatch "^sha256:[0-9a-f]{64}$") { throw "Artifact Registry returned no valid digest for '$taggedImage'." }
$immutableImage = "$Region-docker.pkg.dev/$ProjectId/$RepositoryName/$ImageName@$digest"

$workerExists = Test-GcloudResource run services describe $WorkerServiceName --project $ProjectId --region $Region
$mcpExists = Test-GcloudResource run services describe $ServiceName --project $ProjectId --region $Region
if ((-not $workerExists -or -not $mcpExists) -and -not $Promote) { throw "The first deployment has no prior revisions. Re-run with -Promote after reviewing the bootstrap exception." }

Write-Host "[2/7] Deploying the private worker candidate..." -ForegroundColor Cyan
$workerUrl = ""
if ($workerExists) { $workerUrl = [string](Get-ServiceDocument $WorkerServiceName).status.url }
if (-not $workerUrl) {
  Deploy-WorkerCandidate "$shortSha-worker-bootstrap-$revisionNonce" "" $true
  $workerUrl = [string](Get-ServiceDocument $WorkerServiceName).status.url
}
$workerEndpoint = "$workerUrl/internal/jobs/run"
Deploy-WorkerCandidate "$shortSha-worker-candidate-$revisionNonce" $workerEndpoint $true
$workerDocument = Get-ServiceDocument $WorkerServiceName
$workerRevision = Get-TaggedRevision $workerDocument "candidate"
if (-not $workerRevision) { throw "The worker candidate revision could not be resolved." }
Invoke-Gcloud run services add-iam-policy-binding $WorkerServiceName --project $ProjectId --region $Region --member "serviceAccount:$tasksServiceAccount" --role roles/run.invoker --condition None --quiet

Write-Host "[3/7] Discovering the public MCP candidate tag URL..." -ForegroundColor Cyan
$serviceUrl = ""
$candidateUrl = ""
if ($mcpExists) {
  $serviceDocument = Get-ServiceDocument $ServiceName
  $serviceUrl = [string]$serviceDocument.status.url
  $candidateUrl = Get-TaggedUrl $serviceDocument "candidate"
}
if (-not $candidateUrl) {
  $bootstrapHosts = if ($serviceUrl) { ([Uri]$serviceUrl).Host } else { "" }
  Deploy-McpCandidate "$shortSha-bootstrap-$revisionNonce" $serviceUrl $bootstrapHosts $workerEndpoint $true
  $serviceDocument = Get-ServiceDocument $ServiceName
  $serviceUrl = [string]$serviceDocument.status.url
  $candidateUrl = Get-TaggedUrl $serviceDocument "candidate"
  if (-not $candidateUrl) { throw "Cloud Run did not publish a candidate tag URL." }
}

Write-Host "[4/7] Deploying the hardened MCP candidate by digest with zero traffic..." -ForegroundColor Cyan
$serviceHost = ([Uri]$serviceUrl).Host
$candidateHost = ([Uri]$candidateUrl).Host
Deploy-McpCandidate "$shortSha-candidate-$revisionNonce" $serviceUrl "$serviceHost,$candidateHost" $workerEndpoint $true
$serviceDocument = Get-ServiceDocument $ServiceName
$candidateUrl = Get-TaggedUrl $serviceDocument "candidate"
$mcpRevision = Get-TaggedRevision $serviceDocument "candidate"
if (-not $candidateUrl -or -not $mcpRevision) {
  throw "The hardened MCP candidate URL/revision could not be resolved."
}

Write-Host "[5/7] Smoking /healthz, bearer enforcement, and the eight-tool contract..." -ForegroundColor Cyan
$health = Invoke-RestMethod -Uri "$candidateUrl/healthz" -Method Get
if (-not $health.ok) { throw "Candidate health response did not report ok=true." }
Test-HttpStatus "$candidateUrl/mcp" 401
$accessToken = (& gcloud secrets versions access $accessVersion --secret steam-mcp-access-token --project $ProjectId).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($accessToken)) { throw "Could not read the pinned bearer secret version." }
$priorSmokeToken = $env:MCP_SMOKE_ACCESS_TOKEN
try {
  $env:MCP_SMOKE_ACCESS_TOKEN = $accessToken
  & uv run --quiet --no-project --with "mcp==2.1.1" --with "httpx2==2.12.0" `
    python (Join-Path $PSScriptRoot "smoke-cloud-run.py") `
    --url "$candidateUrl/mcp" `
    --app-id $SmokeAppId
  if ($LASTEXITCODE -ne 0) { throw "Protocol-aware Steam candidate smoke failed." }
}
finally {
  if ($null -eq $priorSmokeToken) { Remove-Item Env:MCP_SMOKE_ACCESS_TOKEN -ErrorAction SilentlyContinue }
  else { $env:MCP_SMOKE_ACCESS_TOKEN = $priorSmokeToken }
}

Write-Host "[6/7] Candidate smoke passed." -ForegroundColor Green
if ($Promote) {
  Invoke-Gcloud run services update-traffic $WorkerServiceName --project $ProjectId --region $Region --to-tags "candidate=100" --quiet
  Invoke-Gcloud run services update-traffic $ServiceName --project $ProjectId --region $Region --to-tags "candidate=100" --quiet
  Write-Host "Promoted worker first, then MCP, to 100%." -ForegroundColor Green
}
else { Write-Host "Both candidates remain at 0%; promote them only after approval." -ForegroundColor Yellow }

if ($Promote) {
  Write-Host "[7/7] Saving the promoted bearer credential for this Windows account..." -ForegroundColor Cyan
  [Environment]::SetEnvironmentVariable($TokenEnvironmentVariable, $accessToken, "User")
  Set-Item -Path "Env:$TokenEnvironmentVariable" -Value $accessToken
}
else {
  Write-Host "[7/7] Candidate credential was not persisted because production was not promoted." -ForegroundColor Yellow
}
Write-Host "Image:       $immutableImage"
Write-Host "MCP rev:     $mcpRevision"
Write-Host "Worker rev:  $workerRevision"
Write-Host "Candidate:   $candidateUrl/mcp"
Write-Host "Production:  $serviceUrl/mcp"
Write-Host "Worker:      $workerEndpoint (private, Tasks OIDC only)"
Write-Host "Health:      $serviceUrl/healthz"
Write-Host "Access sec:  steam-mcp-access-token:$accessVersion"
Write-Host "Worker sec:  steam-mcp-worker-token:$workerTokenVersion"
Write-Host "Cursor sec:  steam-mcp-cursor-secret:$cursorSecretVersion"
if ($steamApiVersion) { Write-Host "Steam API:   steam-web-api-key:$steamApiVersion" }
