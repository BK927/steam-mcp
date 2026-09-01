[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ProjectId,

  [string]$Region = "asia-northeast1",
  [string]$ServiceName = "steam-mcp",
  [string]$ServiceAccountName = "steam-mcp-runner",
  [string]$TokenEnvironmentVariable = "STEAM_MCP_ACCESS_TOKEN",
  [switch]$ConfigureSteamApiKey
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
  & gcloud @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "gcloud command failed with exit code $LASTEXITCODE."
  }
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

function New-RandomHex {
  param([int]$ByteCount = 32)
  $bytes = New-Object byte[] $ByteCount
  [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
  return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Test-SecretExists {
  param([string]$Name)
  & gcloud secrets describe $Name --project $ProjectId --format "value(name)" *> $null
  return $LASTEXITCODE -eq 0
}

function Set-SecretValue {
  param(
    [string]$Name,
    [string]$Value
  )

  if (-not (Test-SecretExists $Name)) {
    Invoke-Gcloud secrets create $Name `
      --project $ProjectId `
      --replication-policy automatic `
      --quiet
  }

  $Value | & gcloud secrets versions add $Name `
    --project $ProjectId `
    --data-file=- `
    --quiet
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to add a version to Secret Manager secret '$Name'."
  }
}

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  throw "Google Cloud CLI (gcloud) is not installed or is not on PATH."
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServiceAccount = "$ServiceAccountName@$ProjectId.iam.gserviceaccount.com"
$ProjectNumber = (& gcloud projects describe $ProjectId --format "value(projectNumber)").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ProjectNumber)) {
  throw "Google Cloud project '$ProjectId' was not found or is not accessible."
}
$BuildServiceAccount = "$ProjectNumber-compute@developer.gserviceaccount.com"

Write-Host "[1/6] Selecting project and enabling APIs..." -ForegroundColor Cyan
Invoke-Gcloud config set project $ProjectId
Invoke-Gcloud services enable `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  artifactregistry.googleapis.com `
  secretmanager.googleapis.com `
  iam.googleapis.com `
  --project $ProjectId

Write-Host "[2/6] Preparing Cloud Run identities..." -ForegroundColor Cyan
& gcloud iam service-accounts describe $ServiceAccount --project $ProjectId *> $null
if ($LASTEXITCODE -ne 0) {
  Invoke-Gcloud iam service-accounts create $ServiceAccountName `
    --project $ProjectId `
    --display-name "Steam MCP Cloud Run runtime"
}

$BuildIdentityFound = $false
for ($attempt = 1; $attempt -le 12; $attempt++) {
  & gcloud iam service-accounts describe $BuildServiceAccount --project $ProjectId *> $null
  if ($LASTEXITCODE -eq 0) {
    $BuildIdentityFound = $true
    break
  }
  Start-Sleep -Seconds 5
}
if (-not $BuildIdentityFound) {
  throw "The default Cloud Build identity '$BuildServiceAccount' was not created. Wait a minute and run the script again."
}

Invoke-Gcloud projects add-iam-policy-binding $ProjectId `
  --member "serviceAccount:$BuildServiceAccount" `
  --role "roles/run.builder" `
  --condition=None `
  --quiet

Write-Host "[3/6] Creating private server credentials..." -ForegroundColor Cyan
$McpAccessToken = New-RandomHex 32
Set-SecretValue -Name "steam-mcp-access-token" -Value $McpAccessToken

if ($ConfigureSteamApiKey) {
  $SteamApiKeySecure = Read-Host "Steam Web API key (leave empty for keyless deployment)" -AsSecureString
  $SteamApiKey = Get-PlainText $SteamApiKeySecure
  if (-not [string]::IsNullOrWhiteSpace($SteamApiKey)) {
    Set-SecretValue -Name "steam-web-api-key" -Value $SteamApiKey.Trim()
  }
  $SteamApiKey = $null
  $SteamApiKeySecure = $null
}
$HasSteamApiKey = Test-SecretExists "steam-web-api-key"

Invoke-Gcloud projects add-iam-policy-binding $ProjectId `
  --member "serviceAccount:$ServiceAccount" `
  --role "roles/secretmanager.secretAccessor" `
  --condition=None `
  --quiet

Write-Host "[4/6] Building and deploying to Cloud Run..." -ForegroundColor Cyan
$SecretMappings = "MCP_ACCESS_TOKEN=steam-mcp-access-token:latest"
if ($HasSteamApiKey) {
  $SecretMappings += ",STEAM_API_KEY=steam-web-api-key:latest"
}

Push-Location $ProjectRoot
try {
  Invoke-Gcloud run deploy $ServiceName `
    --project $ProjectId `
    --region $Region `
    --source . `
    --allow-unauthenticated `
    --service-account $ServiceAccount `
    --port 8080 `
    --cpu 1 `
    --memory 512Mi `
    --concurrency 10 `
    --timeout 300 `
    --min 0 `
    --max 1 `
    --update-env-vars "MCP_TRANSPORT=http,HEALTH_PATH=/health,MCP_ALLOW_UNAUTHENTICATED=false" `
    --update-secrets $SecretMappings `
    --quiet
}
finally {
  Pop-Location
}

Write-Host "[5/6] Pinning the public URL and Host validation..." -ForegroundColor Cyan
$ServiceUrl = (& gcloud run services describe $ServiceName `
  --project $ProjectId `
  --region $Region `
  --format "value(status.url)").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ServiceUrl)) {
  throw "Deployment succeeded, but the Cloud Run service URL could not be read."
}
$ServiceHost = ([Uri]$ServiceUrl).Host
Invoke-Gcloud run services update $ServiceName `
  --project $ProjectId `
  --region $Region `
  --update-env-vars "PUBLIC_BASE_URL=$ServiceUrl,MCP_ALLOWED_HOSTS=$ServiceHost" `
  --quiet

Write-Host "[6/6] Saving the plugin credential for this Windows account..." -ForegroundColor Cyan
[Environment]::SetEnvironmentVariable($TokenEnvironmentVariable, $McpAccessToken, "User")
Set-Item -Path "Env:$TokenEnvironmentVariable" -Value $McpAccessToken

Write-Host "Deployment complete." -ForegroundColor Green
Write-Host "MCP URL:    $ServiceUrl/mcp"
Write-Host "Health URL: $ServiceUrl/health"
Write-Host "Credential: saved as user environment variable $TokenEnvironmentVariable"
Write-Host "Steam API:  $(if ($HasSteamApiKey) { 'configured' } else { 'keyless mode (22 tools remain available)' })"
