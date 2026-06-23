# Multi-Server Setup Commands

Use this checklist for every Discord server that will run the bot.

## Deploy Order

1. Invite the bot to the server.
2. Add the server ID to the deploy environment variable:

```text
GUILD_IDS=server1_id,server2_id,server3_id
```

3. Redeploy or restart the bot so slash commands sync into that server.
4. Run the setup commands below inside the server.

## Tournament Setup

```text
/config-roles staff_role:@TournamentStaff caster_role:@Caster
```

Sets the server-specific tournament staff role and caster role.

- `staff_role`: role allowed to run tournament admin commands, besides Discord administrators.
- `caster_role`: role allowed to handle casted match links. Optional if the server does not use casting.

```text
/tournament-setup
```

Opens dropdowns to choose where tournament channels are created.

- Tournament/public category: signup and update channels.
- Admin/private category: admin and caster channels.

```text
/config-challonge subdomain:your-community-name
```

Optional. Sets this server's Challonge Community permalink/ID.

Example:

```text
/config-challonge subdomain:ckl-na
```

For a free Community, use the permalink from `challonge.com/communities/ckl-na`.

For a Pro Community with a dedicated subdomain, the same value may also be the subdomain name. The Community must already exist under the bot's Challonge account. If unset, brackets are created under the main Challonge account.

## PUG Setup

```text
/config-pug-roles admin_role:@PUGAdmin helper_role:@PUGHelper na_role:@NA eu_role:@EU captain_role:@Captain spectator_role:@Spectator
```

Sets this server's PUG roles.

- `admin_role`: full PUG access, including setup commands.
- `helper_role`: staff access for normal moderation and match control.
- `na_role`: auto-assigned to NA-linked players.
- `eu_role`: auto-assigned to EU-linked players.
- `captain_role`: gives captain priority.
- `spectator_role`: grants match channel and VC viewing/access.

Minimum recommended PUG role setup:

```text
/config-pug-roles admin_role:@PUGAdmin helper_role:@PUGHelper
```

```text
/pug-setup
```

Creates the PUG category, queue channel, Queue voice channel, and results channel for this server.

```text
/pug-set-account-link channel:#account-link
```

Sets the public channel where players start account linking.

```text
/pug-set-review-channel channel:#link-requests
```

Sets the staff review channel where account link requests are approved or denied.

```text
/pug-set-flags-channel channel:#flags
```

Optional. Sets where automatic performance flags are posted.

```text
/pug-set-flags-log-channel channel:#flags-log
```

Optional. Sets where manual flag and unflag logs are posted.

```text
/pug-bigboard
```

Optional. Posts the persistent large leaderboard in the current channel.

## Verification

```text
/config-show
```

Shows the server's configured roles, categories, and Challonge Community.

After setup, smoke-test the PUG queue in each server:

1. Link or manually add a test account.
2. Join the queue in Server A.
3. Join the queue in Server B.
4. Confirm each server's queue embed updates independently.

## Environment Variables

These are shared by the whole deployment, not per server:

```text
GITHUB_GIST_TOKEN
GITHUB_GIST_ID
CHALLONGE_CLIENT_ID
CHALLONGE_CLIENT_SECRET
SET_REGION
SET_REGION_NAME
```

Use one Gist for all servers. The bot stores multi-server data inside that Gist:

- `tournaments.json`
- `pug_data.json`
- `config.json`

Use `GUILD_IDS` to control which servers receive slash commands.

Use `SET_REGION` to control which Krunker server every hosted match must use.

Example:

```text
SET_REGION=DAL
```

`SET_REGION_NAME` is optional display text for Discord messages. If unset, the bot uses a built-in name for common region codes.

Example:

```text
SET_REGION_NAME=Dallas
```

For Challonge, you can use either:

```text
CHALLONGE_API_KEY
```

or:

```text
CHALLONGE_CLIENT_ID
CHALLONGE_CLIENT_SECRET
```

`CHALLONGE_API_KEY` is usually simpler for Community brackets. If it is set, the bot uses it instead of OAuth.
