import os
import discord
from discord import app_commands

ROLE_ID = int(os.environ.get("ROLE_ID", 1492368556627591248))
SERVER_ID = int(os.environ.get("SERVER_ID", 1481881771736956938))
CASTER_ROLE_ID = int(os.environ.get("CASTER_ROLE_ID", 0))

RAILWAY_BASE = "https://tourney-bot-production.up.railway.app"
WEBHOOK_PORT = 5000

MAPS = ["Bureau", "Lush", "Site", "Industry", "Undergrowth", "Sandstorm", "Burg"]


def guild_object():
    return discord.Object(id=SERVER_ID)


def is_authorized():
    """Check that user has ROLE_ID or is an administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        if any(r.id == ROLE_ID for r in interaction.user.roles):
            return True
        await interaction.response.send_message("You don't have permission to use bot commands.", ephemeral=True)
        return False
    return app_commands.check(predicate)
