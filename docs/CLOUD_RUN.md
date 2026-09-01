# Deploying Steam MCP to Google Cloud Run

This deployment keeps the existing local stdio mode and adds a stateless Streamable HTTP endpoint for Codex plugins.

## What is deployed

- MCP: `https://SERVICE_URL/mcp`
- Health: `https://SERVICE_URL/health`
- Transport: Streamable HTTP
- Access: public Cloud Run ingress plus a separate 64-character bearer token
- Scale: zero minimum instances and one maximum instance

The Steam Web API key is optional. Without it, the store, reviews, pricing, player counts, discovery, and current AppInfo/build/depot tools remain available. Account-specific tools require the key and can only read information that Steam exposes publicly.

## Prerequisites

1. Install Google Cloud CLI and sign in.
2. Select a Google Cloud project with billing enabled.
3. Optionally obtain a Steam Web API key from `https://steamcommunity.com/dev/apikey`.

Docker Desktop is not required. The deployment uses Cloud Build with the repository Dockerfile.

## Deploy

```powershell
cd C:\Users\dead4\repo\steam-mcp

pwsh -ExecutionPolicy Bypass -File .\scripts\deploy-cloud-run.ps1 `
  -ProjectId "YOUR_PROJECT_ID"
```

To configure the optional Steam API key during deployment:

```powershell
pwsh -ExecutionPolicy Bypass -File .\scripts\deploy-cloud-run.ps1 `
  -ProjectId "YOUR_PROJECT_ID" `
  -ConfigureSteamApiKey
```

The script enables the required Google Cloud APIs, creates the runtime identity, stores credentials in Secret Manager, deploys to Cloud Run, pins Host validation to the generated service URL, and saves the MCP bearer token as the Windows user environment variable `STEAM_MCP_ACCESS_TOKEN`.

## Verify

Open a new PowerShell window after deployment:

```powershell
$baseUrl = "https://YOUR_SERVICE_URL"
Invoke-RestMethod "$baseUrl/health"

try {
  Invoke-WebRequest "$baseUrl/mcp"
} catch {
  $_.Exception.Response.StatusCode.value__
}
```

The health endpoint returns `ok: true`; the unauthenticated MCP request returns `401`.

A Codex plugin can use:

```json
{
  "mcpServers": {
    "steam": {
      "type": "http",
      "url": "https://YOUR_SERVICE_URL/mcp",
      "bearer_token_env_var": "STEAM_MCP_ACCESS_TOKEN"
    }
  }
}
```

## Security and cost

- The Steam API key and MCP bearer token are stored in Secret Manager, not the repository.
- The deployment is read-only and does not trade, purchase, post, launch games, or change Steam accounts.
- Cloud Run scales to zero when idle. Builds and retained Artifact Registry images can still create small charges.
- The static bearer token is appropriate for a private personal plugin. A public multi-user plugin should implement standards-based MCP OAuth before publication.
