# Privacy Policy — Steam MCP

_Last updated: 2026-09-01_

Steam MCP is a read-only, self-hosted Model Context Protocol server. You may run
it locally or in infrastructure you control; the project author does not operate a
hosted Steam MCP service. This document explains what it does and does not do with
data.

## What it accesses

When you (or your AI client) invoke a tool, the server makes read-only HTTPS
requests to fixed, allowlisted endpoints:

- `https://api.steampowered.com` (Valve's Steam Web API)
- `https://store.steampowered.com` (Valve's Steam storefront/reviews)
- `https://steamcommunity.com` (Valve's public community/market endpoints)
- `https://api.steamcmd.net` (a community-operated mirror of public SteamCMD
  AppInfo, used only by current product/build/branch/depot tools)

Requests may include:

- **Your Steam Web API key**, read from the key you supply at install time (or a
  local `.env` / environment variable). The key is sent only to Valve, only as
  required to authenticate API calls. It is never transmitted anywhere else.
- **SteamIDs / vanity names / app IDs** you ask about, passed as request
  parameters to Valve where the requested tool requires them.
- **A numeric app ID** sent to `api.steamcmd.net` only when you invoke a current
  AppInfo/build/branch/depot tool (or `steam_analyze_game`). Steam API keys,
  SteamIDs, libraries, review corpora, and other account data are never sent to
  that mirror.
- **Public review data** returned by Valve, which can include review text, public
  reviewer SteamIDs, playtime, helpfulness votes, purchase/free/early-access flags,
  and developer responses. Review pages are processed in memory and are not
  persisted. Returned reviewer SteamIDs are omitted by default; callers must
  explicitly set `include_author_id=true` when an analysis genuinely needs them.

## What it does NOT do

- It does **not** transmit data to the author and contains no analytics or
  telemetry. Most data goes directly between your MCP deployment and Valve. The
  current AppInfo/build tools additionally send the requested numeric app ID to the
  disclosed third-party `api.steamcmd.net` mirror.
- It is **read-only**: it cannot send messages, change your status, modify your
  account, launch games, or make purchases.
- It does **not** write your API key to any file it ships. The key stays in your
  local configuration.

## Storage, sharing, and retention

- **Local storage:** none. The MCP persists nothing to disk. A small in-memory
  cache may hold public, non-account store/app/tag/news/review-summary responses
  and current AppInfo snapshots; it lives only for the running process. AppInfo
  mirror responses are cached for five minutes.
- **Third-party sharing:** only a requested numeric app ID is sent to the disclosed
  `api.steamcmd.net` mirror for current technical metadata. The mirror is an
  independent service that maintains its own AppInfo database and may have its own
  logging and retention practices. No Steam credential or account identifier is
  sent there by this MCP.
- **Local retention:** nothing is retained between process restarts. Large review
  scans keep aggregate counters and bounded representative samples rather than the
  complete corpus. Invisible/control characters are removed from returned review
  and developer-response text.
- **If the server asks who you are:** when no `STEAM_USER` is configured and your
  client supports elicitation, the server may ask once for your Steam account so
  the "about me" tools have someone to talk about. That answer is held in memory
  for the life of the process — so you are asked once rather than once per call —
  and is never written to disk or sent anywhere but Valve's endpoints. It is a
  public profile name, not a credential. Restarting the server forgets it; setting
  `STEAM_USER` stops it being asked at all.

## Data visibility

The account tools can only read data that Valve exposes. Friends lists, owned
games, and achievements are returned only when the target Steam profile's privacy
settings make them **Public**. Store reviews and their accompanying reviewer
metadata are public storefront data. The AppInfo mirror exposes current public
SteamCMD metadata and may report that an access token is missing for restricted
apps; the MCP cannot bypass that restriction or access private profile data.

## Your responsibilities

- Keep your Steam Web API key secret. Treat it like a password.
- Your use of the Steam Web API is governed by Valve's
  [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).

## Contact

Issues and questions: https://github.com/Sarg338/steam-mcp/issues

_This project is not affiliated with or endorsed by Valve Corporation._
