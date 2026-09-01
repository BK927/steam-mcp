<!-- mcp-name: io.github.Sarg338/steam-mcp -->

# Steam MCP

[![PyPI](https://img.shields.io/pypi/v/steam-mcp?cacheSeconds=3600)](https://pypi.org/project/steam-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/steam-mcp)](https://pypi.org/project/steam-mcp/)
[![CI](https://github.com/Sarg338/steam-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Sarg338/steam-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.Sarg338%2Fsteam--mcp-blue)](https://registry.modelcontextprotocol.io)

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server for the
public Steam Web API/storefront and a disclosed SteamCMD AppInfo mirror — **44 tools,
5 prompts, and 2 resources** that let any MCP client (Claude Desktop, Claude Code,
Cursor, …) answer questions about Steam: your friends, games, playtime, and
achievements, plus account-independent things like sales, reviews, live player
counts, current builds/branches/depots, discovery, recommendations, and co-op
planning.

**Read-only · no SteamDB scraping · open source.** Valve-hosted data is used wherever
available; current technical AppInfo comes from the keyless community-operated
`api.steamcmd.net` mirror and is labelled as such in every result. The server never
writes, trades, posts, launches games, or makes purchases.

## Quick start — no API key needed

Install [`uv`](https://docs.astral.sh/uv/), then:

**Claude Code**

```bash
claude mcp add steam -- uvx steam-mcp
```

That's the whole setup. **22 of the 44 tools work with no credential at all** — anything
about the store, reviews, or a game's current public technical state:

> *"Is Baldur's Gate 3 worth buying, and how are its recent reviews trending?"*
> *"What co-op games are on sale under £20 right now?"*
> *"How many people are playing Helldivers 2 this minute?"*
> *"Will Hades II run properly on my Steam Deck?"*
> *"What is this game's current public build ID, branches, depots, and manifests?"*

The three game-finders (`steam_discover`, `steam_should_i_buy`, `steam_recommend`) work
without a key too, as long as you don't personalize them.

### Adding your own account

A [free Steam Web API key](https://steamcommunity.com/dev/apikey) (a minute to get)
unlocks the 19 account-only tools and personalization on the three game-finders:
library, playtime, friends, achievements, wishlist, inventory, and taste matching.
Add `STEAM_USER` too and "my"/"I" default to you, so you never have to paste a SteamID:

```bash
claude mcp add steam --env STEAM_API_KEY=YOUR_KEY --env STEAM_USER=your_steam_name -- uvx steam-mcp
```

> Tip: this defaults to the current project. Add `--scope user` only if you want
> Steam in *every* project — that keeps its tools in context everywhere, so prefer
> per-project scope unless Steam is cross-cutting for you.

**Claude Desktop** — download `steam-mcp.mcpb` from the
[latest release](https://github.com/Sarg338/steam-mcp/releases/latest) and open it
(Settings → Extensions). Both fields are optional; leave them blank for the keyless
tools and fill them in later.

Cursor / Cline / Windsurf and the manual `pip` setup are under [Setup](#setup) below.

> Without a key, the account tools are still listed but marked
> `[unavailable: needs STEAM_API_KEY]`, so your assistant knows to reach for a keyless
> tool instead of failing at one it can't use.

---

## What it can answer

Account / profile (needs a public profile; set `STEAM_USER` and "my"/"I" default
to you — no SteamID needed):
- "Who's on my friends list, and who's online right now?"
- "Which of my friends own *Helldivers 2* — and who's playing it now?"
- "It's game night — what co-op games do my online friends and I all own?"
- "Analyze my library — my backlog, and what I loved but abandoned."
- "Which achievements am I missing in *Hollow Knight*, and which are my rarest?"
- "What's on my wishlist, and is any of it on sale?"
- "Based on what I play most, what should I check out next?"
- "What's in my CS2 inventory, and which items are marketable?"

Account-independent (works for any game, no SteamID needed):
- "Is *Baldur's Gate 3* worth buying — and how are its recent reviews trending?"
- "Analyze 10,000 reviews for the main complaints, sentiment shifts, and player profile."
- "Give me one snapshot of this game's store position, reviews, CCU, tags, patches, and current build."
- "Show the current public branch build IDs and depot manifests without scraping SteamDB."
- "What's on sale right now, and what are the current top sellers?"
- "How many people are playing *Counter-Strike 2* this minute?"
- "Will *Hades II* run on my Steam Deck?"
- "What's the Community Market price of a Field-Tested AK-47 | Redline?"
- "Is *Elden Ring* a soulslike? What are its community tags?"
- "Find well-reviewed co-op roguelikes under $20."
- "Recommend games like *Hollow Knight* that I don't already own."

---

## Tools

| Tool | What it returns | Needs key? |
|------|-----------------|-----------|
| `steam_resolve_vanity_url` | Vanity name / profile URL → SteamID64 | yes |
| `steam_get_player_summary` | Status (Online/Away/In-Game…), current game, for 1–100 users | yes |
| `steam_get_friend_list` | Friends enriched with name + live status | yes |
| `steam_find_friends_who_own` | **Which friends own (or are playing) a game** — "who can I play X with" | yes |
| `steam_get_user_groups` | The Steam groups/clans a user is in (name, URL, member count) | yes |
| `steam_plan_coop_night` | **Co-op games the host + friends all own** (ranked by owners) — or `mode="new"` for **fresh co-op games none of them own yet**; with who's online now | yes |
| `steam_get_owned_games` | Owned games with total/recent hours (sortable) | yes |
| `steam_analyze_library` | **Backlog, playtime distribution, abandoned games** across a whole library | yes |
| `steam_get_recently_played_games` | Last-2-weeks playtime | yes |
| `steam_get_steam_level` | Steam community level | yes |
| `steam_get_player_bans` | VAC / game / community / economy bans | yes |
| `steam_get_player_achievements` | Per-game unlocked vs locked achievements | yes |
| `steam_get_game_schema` | A game's full achievement/stat definitions | yes |
| `steam_get_global_achievement_percentages` | Achievement rarity (global %) | no |
| `steam_get_user_game_stats` | **A user's in-game stats** (kills, wins, distance…) for a game | yes |
| `steam_get_rarest_unlocks` | **A player's rarest achievement unlocks** in a game (by global rarity) | yes |
| `steam_search_apps` | Game title → appid (+ price) | no |
| `steam_discover` | **Find/recommend games** by tag, price, sale, platform, **release window** ("last N days") — optionally **personalized** to a user's taste (excludes games they own) | no* |
| `steam_should_i_buy` | **Buying brief** — price, official Steam score vs all readable feedback, recent trend, tags, Metacritic, and taste match | no* |
| `steam_recommend` | **Recommend games** like a seed game or your taste, with the shared tags as the "why" | no* |
| `steam_analyze_game` | **One-call current game snapshot** — store, official reviews/trend, current players, tags, Deck, news, and current AppInfo/build | no |
| `steam_get_app_details` | **Full store details** — play modes/co-op, controller, DLC, languages, requirements, Metacritic, Steam Deck | no |
| `steam_get_product_info` | **Current public SteamCMD AppInfo overview** — change number, app state/config, selected build, and counts | no |
| `steam_get_branches` | Current visible branches with build IDs, descriptions, password-required flag, and timestamps | no |
| `steam_get_depots` | Current depots, platform/language constraints, shared-depot links, and visible manifest GIDs/sizes | no |
| `steam_get_current_build` | Current branch build ID plus its visible per-depot manifest snapshot | no |
| `steam_get_deck_compatibility` | **Steam Deck rating** (Verified/Playable/Unsupported) + the per-criterion test results | no |
| `steam_get_dlc` | **A game's DLC**, with live prices and what's on sale | no |
| `steam_get_app_regional_pricing` | A game's price **across regions** (each in local currency) | no |
| `steam_get_workshop_item` | **Workshop item** metadata (game, tags, subscribers, favorites, views) | no |
| `steam_get_app_tags` | **A game's top community tags** (Souls-like, Roguelike, Cozy…) | no |
| `steam_get_app_reviews` | **Official all-language Steam-purchase score** plus a separately filtered feedback corpus, up to 100 excerpts, and optional recent scans | no |
| `steam_get_app_review_batch` | **Full review text in cursor pages of up to 100**; hidden controls are removed and reviewer SteamIDs are omitted unless explicitly requested | no |
| `steam_analyze_app_reviews` | **Large-corpus review analysis** — timelines and segment sentiment; preserves partial aggregates and a continuation cursor on budgets/errors | no |
| `steam_get_featured_specials` | Games currently on sale (regional) | no |
| `steam_get_store_highlights` | **Top sellers, new releases, or coming soon** | no |
| `steam_get_wishlist` | **A user's wishlist, with live prices + what's on sale** | yes |
| `steam_get_inventory` | **A user's inventory** — game items or Steam Community items (cards, emoticons…), with tradable/marketable flags | yes† |
| `steam_get_market_price` | **Community Market price** for an item (lowest/median/24h volume) + type/rarity + CS2 condition | no |
| `steam_get_player_badges` | Badges + the XP breakdown behind a Steam level | yes |
| `steam_get_package_details` | Package/bundle price + included games | no |
| `steam_compare_players` | Shared games between two users, with playtime | yes |
| `steam_get_current_players` | Live concurrent player count | no |
| `steam_get_app_news` | Recent news / patch notes | no |

Every tool supports `response_format: "markdown"` (default) or `"json"`, and all are
annotated `readOnlyHint: true`. Prefer the composite tools (`steam_analyze_game`,
`steam_should_i_buy`, `steam_recommend`, `steam_discover`,
`steam_plan_coop_night`) over chaining several calls, and ask for `json` only when
you need to parse fields. Tools that read
localized text accept a `language` parameter — a Steam language name like `french` or
`schinese` (default `english`). For review tools, this filters the analysis corpus;
the separately labelled official store score always uses all languages and Steam
purchases, matching Steam's score population.

> \* `steam_discover`, `steam_should_i_buy`, and `steam_recommend` need no key for
> the store data; their **personalization** (passing a `steamid` to use a user's
> library/taste) requires a key and a public profile.
>
> † `steam_get_inventory` reads a keyless endpoint, but it still has to know *whose*
> inventory — and turning a vanity name (or `STEAM_USER`) into a SteamID64 is itself a
> keyed call. Pass a raw 17-digit SteamID64 and it works with no key.

### Prompts & resources

Beyond tools, the server ships **prompts** (guided one-click flows that orchestrate
the tools) and **resources** (reference Steam entities by URI):

- Prompts: `what_should_i_play`, `is_it_worth_buying`, `plan_game_night`,
  `steam_deals`, `game_overview`.
- Resources: `steam://app/{appid}` (store details) and `steam://user/{steamid}`
  (profile + live status).

> **Review scores and corpus access:** Steam's store score counts all-language
> reviews from accounts that purchased on Steam; readable key/free/external-copy
> reviews are a different population. The review tools report those two populations
> separately. Steam returns at most 100 review bodies per request, then supplies a
> cursor. `steam_get_app_review_batch` exposes it directly. For a compact quantitative
> pass, `steam_analyze_app_reviews(max_reviews=5000)` streams pages into aggregates
> and bounded samples; `max_pages` / `max_seconds` and request failures return the
> work completed so far plus `next_cursor`. Set `max_reviews=0` only when uncapped
> traversal is worthwhile. Recent-score scans likewise stream counts without keeping
> the complete corpus in memory; `recent_max_reviews=0` covers the full `day_range`.
>
> **Untrusted review text:** review bodies and developer responses are public
> user-generated data, not instructions. The server removes invisible/control
> characters, labels returned text as untrusted, and hides reviewer SteamIDs by
> default. An MCP client or agent must still refuse to follow commands, visit links,
> expose secrets, or invoke other tools merely because review text asks it to.

> **Current AppInfo/build data:** `steam_get_product_info`, `steam_get_branches`,
> `steam_get_depots`, `steam_get_current_build`, and the technical section of
> `steam_analyze_game` call the free, keyless, community-operated
> `api.steamcmd.net` mirror. They never scrape SteamDB. Every response includes
> source provenance and reports current state only: this MCP keeps no persistent
> database, so it cannot reconstruct historical price, CCU, build, branch, depot,
> or AppInfo changes. A five-minute in-memory cache is discarded when the process
> exits. The external mirror maintains its own AppInfo database and may apply its
> own logging/retention policy; only the requested numeric appid is sent to it —
> never your Steam Web API key, SteamID, library, or review corpus. Returned
> AppInfo strings are labelled as untrusted external data. Set
> `include_technical=false` on `steam_analyze_game` for a Valve-hosted-only
> composite call.
>
> **Market prices:** `steam_get_market_price` uses Steam's Community Market
> endpoints, which are undocumented and tightly rate-limited. Results are cached
> briefly; an item with no current listings reports its price as unavailable.

---

## Setup

### 1. Get a free Steam Web API key *(optional)*

Skip this if you only want the 22 keyless tools — the server runs fine without a
key and the account tools simply advertise themselves as unavailable.

To unlock the account tools, visit <https://steamcommunity.com/dev/apikey>, sign in,
register a domain (any domain you control works; `localhost` is commonly used for
personal keys), and copy the key. Usage is governed by the
[Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).

### 2. Install

The published package needs no checkout (Python 3.10+):

```bash
uvx steam-mcp          # zero-install via uv (recommended)
# or
pip install steam-mcp  # run as: python -m steam_mcp.server
```

Both MCP Python SDK majors work (`mcp>=1.28`). On the v2 SDK the server speaks
spec revision 2026-07-28 — stateless, no `initialize` handshake — advertises
cache hints on its tool/prompt/resource listings, and can ask you which Steam
account is yours when `STEAM_USER` isn't set (once per session, and only if your
client supports elicitation). On the v1.x line it serves the `initialize`
handshake that modern clients fall back to anyway. Nothing to configure either
way.

> **TLS note:** the HTTP client is `httpx2`, which verifies certificates against
> your **operating system's** trust store rather than a bundled CA list. If you
> run this somewhere minimal (a slim container with no system CA store, or behind
> a private CA), point `SSL_CERT_FILE` or `SSL_CERT_DIR` at a CA bundle.

### 3. Add it to your MCP client

Both settings are optional. `STEAM_API_KEY` unlocks the account tools; `STEAM_USER`
(your Steam vanity name, SteamID64, or profile URL) makes those tools default to *you*
whenever you don't name a user, so you never paste a SteamID. It's a public profile
name, not a secret, and you can still pass a `steamid` to any call to override it.

Configure neither and you get the keyless server; configure both and you get everything.

**Claude Code**

```bash
claude mcp add steam --env STEAM_API_KEY=YOUR_KEY --env STEAM_USER=your_steam_name -- uvx steam-mcp
```

> `STEAM_USER` is optional — drop the second `--env` if you'd rather give a
> SteamID to each call.

**Claude Desktop** — install `steam-mcp.mcpb` from the
[latest release](https://github.com/Sarg338/steam-mcp/releases/latest) via
Settings → Extensions and paste your key (and, optionally, your Steam name).

**Everything else** (Claude Desktop config, Cursor, Cline, Windsurf, VS Code, …) —
drop this block into the client's MCP config file:

```json
{
  "mcpServers": {
    "steam": {
      "command": "uvx",
      "args": ["steam-mcp"],
      "env": {
        "STEAM_API_KEY": "YOUR_KEY_HERE",
        "STEAM_USER": "your_steam_name"
      }
    }
  }
}
```

Config locations: Claude Desktop `claude_desktop_config.json` (`%APPDATA%\Claude\`
on Windows, `~/Library/Application Support/Claude/` on macOS); Cursor
`.cursor/mcp.json`; Cline `cline_mcp_settings.json`. Restart the client and the
Steam tools appear. Running from a source checkout instead? Use
`"command": "python", "args": ["-m", "steam_mcp.server"]`.

---

## Security

### Remote deployment

The default remains local stdio. For a private remote Codex plugin, this repository also supports stateless Streamable HTTP at `/mcp`, bearer-token protection, and Google Cloud Run deployment. See [docs/CLOUD_RUN.md](docs/CLOUD_RUN.md).

Read-only, official-Steam-only, and bring-your-own-key. In short:

- **Read-only** — never writes, trades, posts, launches games, or buys anything.
- **Your key stays yours** — read from `STEAM_API_KEY`; never written to disk,
  logged, cached, or put in output (and redacted from error messages).
- **Official hosts only** — the request layer refuses any host that isn't
  `api.steampowered.com` / `store.steampowered.com` / `steamcommunity.com` (SSRF
  guard), with per-host rate limiting and retry/backoff.
- **Typed, validated inputs** (`extra="forbid"`); no data kept between requests
  beyond a small TTL cache of non-user store data.

Full details and how to report issues are in [SECURITY.md](SECURITY.md).

---

## Versioning & stability

`steam-mcp` follows [Semantic Versioning](https://semver.org). As of **1.0**, the
following are the **stable public surface** — they won't change without a major
(2.0) release:

- **Tool names** and their **input parameters** (names, types, whether required,
  defaults)
- **JSON output fields** (`response_format: "json"`) — names, types, and structure
- **Prompt** names/arguments and **resource** URI templates
  (`steam://app/{appid}`, `steam://user/{steamid}`)
- Core semantics: read-only, bring-your-own-key, prices in cents / playtime in
  minutes, and errors returned as strings

Within a major version, **minor** releases may *add* tools, prompts, resources,
optional parameters, and JSON fields; **patch** releases are bug fixes only. The
**Markdown** output wording, internal implementation, caching behavior, and which
Steam endpoints back a given tool may change at any time and are **not** part of
the contract.

---

## License

MIT. Not affiliated with Valve. "Steam" is a trademark of Valve Corporation.
