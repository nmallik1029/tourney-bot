import os
import discord
from discord import app_commands

ROLE_ID = int(os.environ.get("ROLE_ID", 1492368556627591248))
SERVER_ID = int(os.environ.get("SERVER_ID", 1481881771736956938))
CASTER_ROLE_ID = int(os.environ.get("CASTER_ROLE_ID", 0))


def _parse_guild_ids() -> list[int]:
    """Servers the bot's commands are synced to. Set GUILD_IDS to a comma-separated
    list of guild ids to authorize more servers; defaults to just the original one.
    Commands are synced per-guild (instant), so adding a server here + redeploying
    makes every command appear in it immediately."""
    raw = os.environ.get("GUILD_IDS", "")
    ids = [int(x) for x in raw.replace(" ", "").split(",") if x.strip().isdigit()]
    if SERVER_ID not in ids:
        ids.append(SERVER_ID)
    return ids


GUILD_IDS = _parse_guild_ids()

RAILWAY_BASE = "https://tourney-bot-production.up.railway.app"
WEBHOOK_PORT = 5000

SET_REGION = os.environ.get("SET_REGION", "DAL").strip().upper() or "DAL"
REGION_NAMES = {
    "AFR": "Africa",
    "BHN": "Bahrain",
    "BRZ": "Brazil",
    "DAL": "Dallas",
    "FRA": "Frankfurt",
    "HKG": "Hong Kong",
    "IND": "India",
    "JPN": "Japan",
    "LON": "London",
    "MIA": "Miami",
    "NY": "New York",
    "SIN": "Singapore",
    "SV": "Silicon Valley",
    "SYD": "Sydney",
    "TKY": "Tokyo",
}
SET_REGION_NAME = os.environ.get("SET_REGION_NAME", "").strip() or REGION_NAMES.get(SET_REGION, SET_REGION)

MAPS = ["Bureau", "Lush", "Site", "Industry", "Undergrowth", "Sandstorm", "Burg"]


def guild_object():
    return discord.Object(id=SERVER_ID)


def is_authorized():
    """Check that the user has this guild's configured mod role or is an administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        from core.guild_config import mod_role_id
        rid = mod_role_id(interaction.guild_id)
        if rid and any(r.id == rid for r in interaction.user.roles):
            return True
        await interaction.response.send_message("You don't have permission to use bot commands.", ephemeral=True)
        return False
    return app_commands.check(predicate)
