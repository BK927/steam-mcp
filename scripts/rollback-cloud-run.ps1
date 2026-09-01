[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ProjectId,
  [Parameter(Mandatory = $true)][ValidatePattern("^[a-z][a-z0-9-]{0,62}$")][string]$McpRevision,
  [Parameter(Mandatory = $true)][ValidatePattern("^[a-z][a-z0-9-]{0,62}$")][string]$WorkerRevision,
  [string]$Region = "asia-northeast1",
  [string]$ServiceName = "steam-mcp",
  [string]$WorkerServiceName = "steam-mcp-worker"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
foreach ($target in @(@($ServiceName, $McpRevision), @($WorkerServiceName, $WorkerRevision))) {
  $targetService = $target[0]
  $targetRevision = $target[1]
  $revisionJson = & gcloud run revisions describe $targetRevision `
    --project $ProjectId --region $Region --format json
  if ($LASTEXITCODE -ne 0) {
    throw "Revision '$targetRevision' was not found in '$Region'."
  }
  $revisionDocument = $revisionJson | ConvertFrom-Json
  $revisionService = [string]$revisionDocument.metadata.labels.'serving.knative.dev/service'
  if ($revisionService -ne $targetService) {
    throw "Revision '$targetRevision' belongs to '$revisionService', not '$targetService'."
  }
}
& gcloud run services update-traffic $ServiceName --project $ProjectId --region $Region --to-revisions "$McpRevision=100" --quiet
if ($LASTEXITCODE -ne 0) { throw "MCP rollback failed; worker traffic was not changed." }
& gcloud run services update-traffic $WorkerServiceName --project $ProjectId --region $Region --to-revisions "$WorkerRevision=100" --quiet
if ($LASTEXITCODE -ne 0) { throw "MCP rolled back, but worker rollback failed." }
Write-Host "Rolled back MCP first and worker second." -ForegroundColor Green
