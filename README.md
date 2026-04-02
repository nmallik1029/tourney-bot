# Tournament Bot — Setup

## Install
```bash
pip install -r requirements.txt
```

## Run
```bash
DISCORD_TOKEN=your_token_here python bot.py
```
Or set it directly in bot.py (not recommended for production).

## Bot Permissions Required
When inviting the bot to your server, it needs:
- `Manage Channels` — to create #signups and #tournament-{id}
- `Send Messages`
- `Embed Links`
- `Read Message History`
- `View Channels`

Use this permission integer when generating your invite URL: **93184**

## Discord Developer Portal
1. Go to https://discord.com/developers/applications
2. Create a new application
3. Go to "Bot" → copy your token
4. Under "Privileged Gateway Intents", enable:
   - Server Members Intent
   - Message Content Intent
5. Invite the bot with the permissions above

## Commands
| Command | Description |
|---|---|
| `/tournament-create` | Opens a form to create a tournament + #signups channel |
| `/tournament-start <id>` | Closes sign-ups and creates the bracket channel |

## Getting a Discord Timestamp for the Date field
1. Go to https://discordtimestamp.com
2. Pick your date/time
3. Copy the `<t:XXXXXXXXXX:F>` format
4. Paste it into the Date field when creating a tournament
