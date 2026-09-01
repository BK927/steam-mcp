# Steam MCP

Steam MCP 2.1.0 is a read-only, compact Steam research server. It intentionally replaces the former wide tool catalog with eight task-oriented tools so clients do not carry dozens of irrelevant schemas in every conversation.

It supports two deployment profiles:

- local `stdio`, with process-local cache and jobs;
- Google Cloud Run Streamable HTTP at `/mcp`, with a separate private worker, Cloud Tasks, Firestore, and Cloud Storage.

Backward compatibility with the pre-2.0 tool names is not provided.

## Public tool surface

| Tool | Use |
| --- | --- |
| `steam_game_get` | Game/store/build/depot/price/player facts for one title |
| `steam_player_get` | Public player/profile/library facts; some fields require `STEAM_API_KEY` |
| `steam_search` | Find games or players with bounded filters and cursor pagination |
| `steam_reviews_get` | Review summaries and bounded review evidence |
| `steam_community_get` | Public community, achievement, friend, or inventory views |
| `steam_analyze` | High-level comparison, recommendation, review, or library analysis |
| `steam_job_get` | Poll a long-running analysis job and retrieve bounded results |
| `steam_job_cancel` | Request cancellation of a queued or running job |

Responses use a common envelope, opaque signed cursors, and a default result budget of 12,288 bytes. The hard result limit is 32,768 bytes. Cursors expire after 86,400 seconds by default. Large work returns a job handle or continuation instead of filling model context.

## Local stdio

Python 3.10 or newer is supported. A Steam Web API key is optional.

```powershell
uvx steam-mcp
```

Generic MCP client configuration:

```json
{
  "mcpServers": {
    "steam": {
      "type": "stdio",
      "command": "uvx",
      "args": ["steam-mcp"],
      "env": {
        "STEAM_API_KEY": "OPTIONAL_STEAM_WEB_API_KEY",
        "STEAM_USER": "OPTIONAL_PUBLIC_PROFILE_REFERENCE"
      }
    }
  }
}
```

From a source checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,gcp]"
.\.venv\Scripts\python -m steam_mcp.server
```

## Google Cloud Run

The production topology keeps Steam and YouTube as separate services and plugins. Steam uses one image for both roles:

```text
Codex -> public steam-mcp (/mcp, bearer)
                     |
                     +-> Cloud Tasks (1 task/s, concurrency 2, attempts 3)
                              |
                              +-> private steam-mcp-worker (OIDC)
                                      |-> Firestore job metadata
                                      '--> private GCS result objects (delete after 7 days)
```

Provision once, then deploy a clean commit:

```powershell
pwsh -File .\scripts\provision-gcp.ps1 -ProjectId "YOUR_PROJECT_ID"
pwsh -File .\scripts\deploy-cloud-run.ps1 -ProjectId "YOUR_PROJECT_ID" -Promote
```

Later deployments build `REGION-docker.pkg.dev/PROJECT/mcp/steam-mcp:GIT_SHA`, resolve its registry digest, create tagged zero-traffic candidates, smoke the public contract, and promote only when `-Promote` is present. Bearer rotation occurs only with `-RotateAccessToken`. See [docs/CLOUD_RUN.md](docs/CLOUD_RUN.md).

Cloud plugin configuration lives in `.mcp.json`; local and cloud profiles are both supported by `scripts/sync-codex-plugin.ps1`. That synchronization script changes the user's plugin installation, so it is not part of tests or deployment.

Hosts that implement OpenAI [Tool Search](https://developers.openai.com/api/docs/guides/tools-tool-search) can defer this server's definitions until Steam work is actually selected. Enable `tool_search` and mark the MCP tool as `defer_loading` in the host/API tool configuration; do not add `defer_loading` to this plugin's `.mcp.json`, which follows the [Codex plugin packaging contract](https://developers.openai.com/plugins/build/plugins).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MCP_TRANSPORT` | `stdio` | `stdio` locally, `http` on Cloud Run |
| `MCP_PATH` | `/mcp` | Streamable HTTP MCP path |
| `HEALTH_PATH` | `/healthz` | Public liveness path |
| `HTTP_MAX_BODY_BYTES` | `2097152` | Maximum HTTP request body (2 MiB) |
| `MCP_ACCESS_TOKEN` | empty | Required bearer secret in HTTP mode |
| `PUBLIC_BASE_URL` | empty | Stable service URL used for Host validation |
| `MCP_ALLOWED_HOSTS` | empty | Additional exact Host values, including candidate tag URL |
| `STEAM_API_KEY` | empty | Optional Steam Web API key |
| `STEAM_USER` | empty | Optional public profile reference |
| `STEAM_CURSOR_SECRET` | bearer fallback | Cursor-signing secret |
| `STEAM_CURSOR_TTL_SECONDS` | `86400` | Cursor validity |
| `STEAM_MAX_RESULT_BYTES` | `12288` | Default bounded result size; hard maximum 32,768 |
| `STEAM_JOB_BACKEND` | `memory` | `memory` locally, `gcp` in Cloud Run |
| `STEAM_PROCESS_ROLE` | `mcp` | `mcp` or private `worker` role from the same image |
| `STEAM_JOB_TTL_SECONDS` | `86400` local | Cloud deployment sets 604,800 seconds |

See [.env.example](.env.example) for the full GCP job adapter variables.

## Security boundary

- Every public tool is read-only. It does not trade, buy, post, launch games, or modify Steam accounts.
- `/mcp` uses a fixed bearer token intended for a private single-operator plugin. It is not a multi-user OAuth authorization server.
- The worker has no unauthenticated Cloud Run invoker. Cloud Tasks uses an OIDC identity; the optional worker header token is defense in depth.
- Secrets are injected from service-specific Secret Manager IAM bindings using numeric versions, never `latest`.
- Outbound requests remain restricted to known Steam-related hosts.

## Development

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest -q
```

Plugin routing fixtures under `docs/evals` are review artifacts. They are not automatically executed against new Codex tasks.

## License

MIT. See [LICENSE](LICENSE), [PRIVACY.md](PRIVACY.md), and [SECURITY.md](SECURITY.md).
