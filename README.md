# Krunker Bi-Weekly Tournament Bot
Coded by: xWater

Managed by: Leshawn, Mucnutty, Klap, Araf, Lvpez, Lunarz, Weed, xWater
---

## Table of Contents
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Environment Variables](#environment-variables)
- [Slash Commands](#slash-commands)
- [Tournament Lifecycle](#tournament-lifecycle)
- [Match Flow](#match-flow)
- [Rehost System](#rehost-system)
- [Casting System](#casting-system)
- [VOD Submission](#vod-submission)
- [Web Pages](#web-pages)
- [Webhooks](#webhooks)
- [Persistence](#persistence)
- [Testing Locally](#testing-locally)

---

## Quick Start

### Install
```bash
pip install -r requirements.txt
```

### Run
```bash
python bot.py
```
Environment variables must be set (see below).

### Discord Developer Portal
1. Create an application at https://discord.com/developers/applications
2. Under **Bot**, copy your token and enable:
   - **Server Members Intent**
   - **Message Content Intent**
3. Under **OAuth2 → URL Generator**, check `bot` + `applications.commands`, give admin perms, invite to your server.

---

## Architecture
```
tourney_bot/
- bot.py                      # Entry point (imports + runs)
- core/
  - bot_instance.py           # Discord client + intents
  - config.py                 # Env vars, constants, auth decorator
  - storage.py                # Gist persistence + shared state
- services/
  - challonge.py              # Challonge REST API wrapper
- utils/
  - helpers.py                # Shared utility functions
- views/
  - registration.py           # Signup + roster edit UI
  - pickban.py                # Pick/ban + hosting + rehost UI
  - vod.py                    # VOD submission UI
- commands/
  - tournament.py             # /tournament-* commands
  - match.py                  # /cast, /bracket
  - testing.py                # /test-* commands
- events/
  - lifecycle.py              # on_ready + error handler
  - messages.py               # Krunker link handling + caster release button
- web/
  - server.py                 # aiohttp app + route registration
  - handlers.py               # All HTTP handlers
  - templates/
    - bracket.html            # Admin bracket page
    - seeding.html            # Drag-to-reorder seeding page
    - dashboard.html          # Tournament dashboard
- scoreboard.py               # Scoreboard image generation
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `SERVER_ID` | Yes | Guild (server) ID for command sync |
| `ROLE_ID` | Yes | Staff/management role ID, required to use admin commands |
| `CASTER_ROLE_ID` | No | Role ID for casters (used by `/cast`) |
| `CHALLONGE_API_KEY` | Yes | Challonge v1 API key |
| `GITHUB_GIST_TOKEN` | Optional | GitHub token for Gist persistence |
| `GITHUB_GIST_ID` | Optional | Gist ID for `tournaments.json` storage |
| `WEBHOOK_URL` | Optional | Overrides the default Railway webhook URL (for local testing) |
| `DASHBOARD_TOKEN` | Optional | Static token for `/tournament-info` dashboard (auto-generated if unset) |

If `GIST_TOKEN` and `GIST_ID` are unset, data is persisted to a local `tournaments.json` file.

---

## Slash Commands

### Tournament Management
| Command | Who | Description |
|---|---|---|
| `/tournament-setup` | Staff | Configure which categories tournament/admin channels are created in |
| `/tournament-create` | Staff | Opens a modal to create a new tournament + `#signups` channel |
| `/tournament-start <id>` | Organizer/Admin | Closes signups; opens the seeding page URL |
| `/tournament-end <id>` | Organizer/Admin | Ends the tournament, cleans up channels, sends VOD submission embeds to top 2 |
| `/tournament-delete <id>` | Organizer/Admin | Hard-deletes a tournament and everything it created |
| `/tournament-info` | Staff | Opens the tournament dashboard (web page) |
| `/tournament-remove-team <id> <team>` | Staff | Manually remove a team from a tournament |

### Match Utilities
| Command | Who | Description |
|---|---|---|
| `/cast <match>` | Casters | Claim a match to cast, routes the game link to a private caster channel |
| `/bracket <id>` | Staff | Posts the bracket URL for a tournament |

### Testing
| Command | Who | Description |
|---|---|---|
| `/test-scoreboard` | Staff | Renders a mock scoreboard image |
| `/test-match` | Staff | Spawns a fake matchroom with pick/ban buttons for UI testing |
| `/test-match-cleanup` | Staff | Removes any test matchrooms created by `/test-match` |

Permissions: any command wrapped with `@is_authorized()` requires either `Administrator` or the `ROLE_ID` role.

---

## Tournament Lifecycle

### 1. Setup (one-time per server)
Run `/tournament-setup` and select:
- **Tournament channels** category → where `#signups`, `#tournament-updates`, matchrooms go
- **Admin channels** category → where `#tournament-admin` and `#caster-links` go

### 2. Create
`/tournament-create` opens a modal asking for:
- Tournament name
- Format (`1v1` / `2v2` / `3v3` / `4v4`)
- Prize pool (e.g. `420 / 210 / 70`)
- Date (Discord timestamp, get one from https://discordtimestamp.com)

Creates a `#signups` channel with a green **Register Your Team** button.

### 3. Sign-ups
Captains click the button to:
- Select all team members (user picker)
- Enter team name + each player's IGN
- The team's registration embed is posted in `#signups` with an **Edit Roster** button (only the submitter can use)

### 4. Start
`/tournament-start <id>` returns a link to the **seeding page**, a drag-to-reorder UI.
Click **Confirm Seeding**; the bot will:
- Create `#tournament-updates`, `#tournament-admin`, `#caster-links`
- Create a **role + category + text/voice channel** for every team, assign players to their team role
- Create the Challonge bracket (double elimination)
- Post the admin bracket URL in `#tournament-admin`

### 5. Run Matches
See [Match Flow](#match-flow). Matches are started from the admin bracket page.

### 6. End
`/tournament-end <id>` will:
- Fetch final standings from Challonge
- Send a **VOD submission embed** to the top 2 teams' text channels (1 week deadline, per-player buttons)
- Delete every other team's role/category/channels
- Delete `#signups`, `#tournament-updates`, `#tournament-admin`, `#caster-links`, all matchrooms
- Keep the top 2 teams' text channels + roles alive until VODs are collected

---

## Match Flow

### Starting a Match
1. On the admin bracket page, click an open match
2. Pick format: **BO1 / BO3 / BO5**
3. Toggle **Casted?** (No/Yes), if Yes, the game link is auto-sent to `#caster-links` for caster routing
4. Click **Start Match**, creates a private matchroom channel for both teams

### Pick/Ban (Step 1/3)
Captains use buttons to ban/pick maps in sequence:
- **BO1**: 6 bans alternating, last map remaining is the map
- **BO3**: 2 bans → 2 picks → 2 bans → last map is decider
- **BO5**: 2 bans → 4 picks → last map is decider

### Hosting (Step 2/3)
The **upper-seeded team** always hosts. The host captain clicks **Host Map 1** which:
- Opens Glorp or Crankshaft client with preconfigured room settings (map, team names, team size, region, webhook URL)
- Posts a visible status message

### Joining (Step 3/3)
The host pastes the Krunker lobby link in the matchroom (region must match `SET_REGION`). The bot auto-detects it and:
- Posts a **Step 3/3 | Join the Game** embed with the link
- Captains/players follow the link and join their team (Alpha / Beta)

### Game End → Webhook
The Krunker client posts a `match_end` payload to `/krunker`:
- Score, players (kills/deaths/score), map, winner
- Bot generates a **scoreboard image**
- Posts "**X takes Map N**" embed with next host button in the matchroom
- When series is decided: posts final combined scoreboard to `#tournament-updates`, reports result to Challonge, cleans up the active match record

---

## Rehost System

If a team needs to sub mid-match, they must rehost the entire map (BO3 rounds can't be shifted mid-game).

**Flow:**
1. Captain clicks the red **Rehost Map X** button (appears next to host buttons once a map has started)
2. A rehost request embed is posted pinging `@Tournament Management` and the other captain
3. Three buttons appear:
   - **{OtherCaptain} | Approve** (green, only the other team's captain can click)
   - **Management | Approve** (green, only staff/admins can click)
   - **Deny** (red, either party can click)
4. As each party approves, their button disables and shows their name
5. When **both** approve: the host embed respawns and the hosting flow restarts for that map
6. On deny: the request is cancelled and a staff ping is suggested

---

## Casting System

### Setup
Casters need the `CASTER_ROLE_ID` role.

### Claim a Match
1. Caster runs `/cast <match>` in any channel
2. Bot routes them to a private channel with the Krunker link for that match
3. Caster reacts when done to release the link

### Marking a Match Casted Upfront
During match creation on the admin bracket page, toggle **Casted? → Yes**. When the host pastes the Krunker link, it is sent to `#caster-links` instead of the matchroom join embed, and casters can react to pull the link for their stream.

---

## VOD Submission

When a tournament ends (`/tournament-end`):
1. Bot fetches Challonge standings (or falls back to grand finals participants if tournament isn't finalized)
2. Top 2 teams each receive a **VOD Submission Required** embed in their team text channel:
   - Title: `VOD Submission Required: {TeamName} ({1st/2nd Place})`
   - Deadline: **1 week** from now, as a Discord timestamp
   - One **Submit VOD | {IGN}** button per player on the team
   - Team role is pinged
3. Players click their own button → modal asks for a VOD link
4. Links are saved to persistent storage and viewable on the dashboard

Button enforcement: each button has the player's Discord ID baked in; clicks from anyone else are rejected with an ephemeral message.

The top 2 teams' text channels, categories, and roles are **not deleted** during `/tournament-end` so the VOD embeds remain accessible.

---

## Web Pages

All served by the embedded `aiohttp` server on port `5000`.

| Path | Description |
|---|---|
| `/admin/{tournament_id}/seeding?token=...` | Drag-to-reorder seeding page shown after `/tournament-start` |
| `/admin/{tournament_id}?token=...` | Live bracket view, click matches to start them, toggle casted, see live scores |
| `/dashboard?token=...` | All tournaments at a glance. Expand each to see info, teams, matches, and **VODs** section (clickable VOD links, pending/submitted indicators, placement badges) |
| `/api/tournament/{id}?token=...` | JSON API for bracket data |
| `/api/dashboard?token=...` | JSON API for dashboard data |
| `/api/match-history/{id}?token=...` | JSON API for match history (per-map scoreboards, player stats) |
| `/krunker` | Webhook endpoint for Krunker `match_end` events |
| `/launch?client={glorp,crankshaft}&...` | Redirects to the chosen Krunker client with room params |

Tokens are generated per tournament (`admin_token`) or via `DASHBOARD_TOKEN` env var.

---

## Webhooks

The bot accepts `POST /krunker` with JSON:
```json
{
  "type": "match_end",
  "map": "Bureau",
  "winner": 1,
  "teams": [
    {"team": 1, "name": "TeamA", "score": 3},
    {"team": 2, "name": "TeamB", "score": 1}
  ],
  "players": [
    {"team": 1, "name": "p1", "score": 42, "kills": 10, "deaths": 4, ...}
  ]
}
```

The bot:
1. Matches teams by name against registered tournament teams (case-insensitive)
2. Finds the active match record by team-name set match
3. Records per-map scoreboard and player stats
4. Posts map result in the matchroom with next-map host button
5. On series completion: posts combined scoreboard collage to `#tournament-updates`, saves full match history, reports the final to Challonge, cleans up the active match

---

## Persistence

All tournament data is stored in a **GitHub Gist** (if `GIST_TOKEN`/`GIST_ID` set), otherwise in a local `tournaments.json`.

**What persists:**
- Tournament metadata, team rosters, IGNs, signup states
- Match history (full per-map scoreboards, player stats, Challonge IDs)
- VOD submissions
- Pending VOD teams (so the submission embeds survive restarts)

**What doesn't:**
- `active_matches`, in-memory only (live matches would need to be restarted after a redeploy)

**Button persistence:**
On `on_ready`, the bot re-registers these views so buttons still work after restarts:
- `SignupView` for tournaments with `open: True`
- `EditRosterView` for every registered team
- `VODSubmissionView` for teams with pending VODs

---

## Testing Locally

To test without disturbing the production bot/signups:

1. Create a second Discord application + bot, invite it to a test server
2. Create `.env.test` with the test bot's token, test `SERVER_ID`, and test `ROLE_ID`
3. Leave `GITHUB_GIST_TOKEN` / `GITHUB_GIST_ID` empty so it uses local `tournaments.json` (production Gist untouched)
4. Run `run_test.bat` (loads `.env.test` then launches `bot.py`)

For full webhook testing locally, tunnel `localhost:5000` with [ngrok](https://ngrok.com) and set `WEBHOOK_URL=https://your-ngrok.ngrok-free.app/krunker` in `.env.test`.

---

## Getting a Discord Timestamp
1. Go to https://discordtimestamp.com
2. Pick date/time
3. Copy the `<t:XXXXXXXXXX:F>` format
4. Paste into the **Date** field when creating a tournament

---

## PUG / In-House Mode

A self-contained 4v4 pick-up-games system that runs in the **same bot** as the tournament side. All PUG code lives in `pug/`, `views/pug_*.py`, `events/pug_listeners.py`, and `web/pug_handlers.py`; tournament behavior is untouched. PUG data is stored separately in `pug_data.json`.

### Roles & Branding
- `PUG_ADMIN_ROLE_ID`, full access, including `/pug-setup` and `/pug-set-account-link`.
- `PUG_HELPER_ROLE_ID`, can use **every** PUG command **except** the two setup commands above.
- `PUG_BRAND`, branding shown in embeds/scoreboards (defaults to **CKL**).
All three are env vars (fall back to `0` / `CKL` in `pug/config.py` if unset).

### Setup
1. Set `PUG_ADMIN_ROLE_ID` (and optionally `PUG_HELPER_ROLE_ID`).
2. Run `/pug-setup`, creates the `Pugs` category, a locked `#queue` channel (with the queue embed), and `#results`. (Match VCs are created on pop.)
3. Account linking: create two channels yourself, then run `/pug-set-account-link #account-link` (posts the info embed + **Link Your Account** button there) and `/pug-set-review-channel #link-requests` (where requests are reviewed).

### Account linking flow (player-driven, admin-approved)
1. A player clicks **Link Your Account** in `#account-link` → a form asks for username(s), their primary, and region.
2. The bot opens a **private thread** for them to upload a Krunker **screenshot** (proof).
3. The request (data + screenshot) is posted to the review channel with **Accept / Deny** buttons.
4. Accept links the account (usernames + primary + region) and DMs the player; Deny prompts for a reason and DMs it. `/link` / `/unlink` still let staff edit links directly. A username can only belong to one player.

### Flow
1. **Queue**, players click **Join Queue** on the `#queue` embed. At 8 players it pops.
2. **Check-in**, a `#queue-NNNN` text + voice channel is created. All 8 are pinged with a 90s countdown; join the voice channel to check in. Stragglers are re-pinged every 10s; at 0s, un-checked players are dropped and replaced from the queue (repeats until full or the queue empties, in which case checked-in players are returned to the queue).
3. **Captains**, the two highest-ELO players (random tiebreak).
4. **Snake draft**, order `C1, C2, C2, C1, C1, C2`; each pick has a 30-second timer. If the captain does not pick in time, the highest-ELO available player is auto-picked. The last player is auto-assigned. The embed lists remaining players highest-ELO first.
5. **Map vote**, a 15-second vote where every player gets one vote (click a map, click again to unvote). Most votes wins (ties random, no votes = random map).
6. **Host**, bot creates Team 1 / Team 2 voice channels and moves players in. The higher-ELO captain clicks **Host Match** (Glorp / CrankShaft), which auto-fills both teams from linked usernames, then pastes the Krunker link (`SET_REGION`) back in the channel.
7. **Result**, the Krunker `match_end` webhook is matched to the PUG by **player identity** (linked usernames; falls back to captain/team names), ELO is updated (opponent-scaled, start 1000), and a result + scoreboard is posted to `#results`. An announce embed is posted in the match channel, then channels are cleaned up after 10s.

### Troubleshooting: result didn't post
If a finished game doesn't post to `#results`, check the bot console logs for `[Pug] match_end:` lines:
- **No `[Pug] match_end:` line at all** → the webhook payload's `type` wasn't `match_end` (the handler bails before PUG sees it). Create a channel named `#test-raw-data` and replay — the bot dumps the raw payload there so you can inspect it.
- **`no PUG match found`** → the players in the payload aren't linked (`/link` them) **or** the match wasn't in the host stage. The log prints the reported team names and the active matches' captains so you can compare.

### Regions & host server
Every `/link` sets a player **region** (`NA/EU/ASIA/OCE/ME`) for player profile/role purposes. Hosted tournament and PUG lobbies use the deployment-configured `SET_REGION` value, defaulting to `DAL`.

`SET_REGION` is baked into the host link and enforced when the host pastes the Krunker lobby link.

### Commands
"Staff" = admin **or** helper role.
| Command | Who | Description |
|---|---|---|
| `/pug-setup` | Admin | Create the PUG channels + queue embed |
| `/pug-set-account-link <channel>` | Admin | Post the linking embed + button in the account-link channel |
| `/pug-set-review-channel <channel>` | Admin | Set where account-link requests are reviewed |
| `/link <member> <username> <region>` | Staff | Link a Krunker username + region (NA/EU/ASIA/OCE/ME) to a player (repeatable; rejects usernames already linked to another player) |
| `/unlink <member> <username>` | Staff | Remove a linked username |
| `/set-primary <member> <username>` | Staff | Make a linked username the player's primary (used in lists + webhook team assignment) |
| `/check <user>` | Staff | Linked usernames, ELO, active ban/no-add (reason/admin/date), recent mod history |
| `/noadd <user> [reason] [hours]` | Staff | Block a player from queueing (blank hours = permanent, this is the "ban") |
| `/unnoadd <user>` | Staff | Remove a no-add |
| `/elo-set <user> <elo>` | Staff | Set a player's ELO |
| `/elo-reset [user]` | Staff | Reset ELO to 1000 **and clear W/L**, one player, or everyone (requires a confirm click) if no user given |
| `/match-fix <match-id> <winner>` | Staff | Manually record a live match's winner (awards ELO) |
| `/cancel <match-id>` | Admin → instant; Captain → asks the other captain to agree | Cancel a live match (no result) |
| `/pug-cleanup` | Staff | Delete match channels orphaned by a restart/crash |
| `/simulate 4v4` | Staff | Test match with 7 fake players (you control both captains) |
| `/leaderboard` | Everyone | Post the leaderboard in chat |
| `/live` | Everyone | Show all live matches (one name per player) |
| `/force-pop` | Staff | Force the queue to pop now with whoever is queued (min 2) |
| `/pug-reset-counter [value]` | Staff | Reset the queue number counter (so test pugs don't keep incrementing) |

### ELO
Everyone starts at **1000**. Win/loss deltas are opponent-scaled (team-average Elo expectation, K=32). Tune `ELO_START` / `ELO_K` in `pug/config.py`.
