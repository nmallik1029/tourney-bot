import os

import discord
from discord import app_commands

# Branding. BRAND (short) is used in embed titles; BRAND_FULL on the scoreboard image.
BRAND = os.environ.get("PUG_BRAND", "CKL")
BRAND_FULL = os.environ.get("PUG_BRAND_FULL", "Competitive Krunker League")

# PUG admin role, full access, incl. /pug-setup. Set via env or replace fallback.
PUG_ADMIN_ROLE_ID = int(os.environ.get("PUG_ADMIN_ROLE_ID", 0))
# PUG helper role, can use every command EXCEPT /pug-setup and /pug-set-account-link.
PUG_HELPER_ROLE_ID = int(os.environ.get("PUG_HELPER_ROLE_ID", 0))

# Region roles, auto-assigned when a player's region is linked (NA/EU only).
PUG_NA_ROLE_ID = int(os.environ.get("PUG_NA_ROLE_ID", 0))
PUG_EU_ROLE_ID = int(os.environ.get("PUG_EU_ROLE_ID", 0))

# Captain-priority role: holders are preferred as captains (then ranked by ELO).
PUG_CAPTAIN_ROLE_ID = int(os.environ.get("PUG_CAPTAIN_ROLE_ID", 0))

# Spectator / caster role: gets VC + channel access (connect/speak/screenshare) on
# every pug match, like players, but cannot interact with the bot or join the game.
PUG_SPECTATOR_ROLE_ID = int(os.environ.get("PUG_SPECTATOR_ROLE_ID", 0))

# Match sizing / timing
MATCH_SIZE = 8          # players per popped match (4v4)
TEAM_SIZE = 4           # players per team
CHECKIN_SECONDS = 90    # how long un-checked-in players have before being dropped
REPING_INTERVAL = 10    # seconds between re-pinging stragglers

# Channel / category names
QUEUE_CHANNEL_NAME = "queue"
QUEUE_VC_NAME = "Queue"
RESULTS_CHANNEL_NAME = "results"
PUG_CATEGORY_NAME = "Pugs"

# ELO
ELO_START = 1000
ELO_K = 32

# Player regions (mandatory at /link). Only NA/EU affect host-server selection.
VALID_REGIONS = ["NA", "EU", "ASIA", "OCE", "ME"]

# Leaderboard size for the ephemeral embed
LEADERBOARD_SIZE = 15

# Auto-flag thresholds for underperformance detection (private invite server).
# Low K/D: flagged when deaths are at/near double the kills (e.g. 12-33, 22-40, 13-30),
# with a floor so tiny samples (e.g. 2-5) don't trip it.
FLAG_KD_RATIO = 1.8        # deaths >= FLAG_KD_RATIO * kills
FLAG_KD_MIN_DEATHS = 15    # ...and at least this many deaths
# Low OBJ: flagged when a player contributes under this share of their team's total OBJ.
FLAG_OBJ_TEAM_SHARE = 0.10


def is_pug_admin():
    """Check that the user has this guild's pug-admin role or is an administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        from core.guild_config import pug_admin_role_id
        rid = pug_admin_role_id(interaction.guild_id)
        if rid and any(r.id == rid for r in interaction.user.roles):
            return True
        await interaction.response.send_message(
            "You don't have permission to use PUG admin commands.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)


def member_is_pug_admin(member: discord.Member) -> bool:
    """Non-decorator variant for use inside button callbacks."""
    if member.guild_permissions.administrator:
        return True
    from core.guild_config import pug_admin_role_id
    rid = pug_admin_role_id(member.guild.id)
    return bool(rid) and any(r.id == rid for r in member.roles)


def member_is_pug_staff(member: discord.Member) -> bool:
    """Admin OR helper, used for everything except the two setup commands."""
    if member_is_pug_admin(member):
        return True
    from core.guild_config import pug_helper_role_id
    rid = pug_helper_role_id(member.guild.id)
    return bool(rid) and any(r.id == rid for r in member.roles)


def is_pug_staff():
    """Check for admin or helper. Use on all commands except /pug-setup and
    /pug-set-account-link (those keep @is_pug_admin())."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if member_is_pug_staff(interaction.user):
            return True
        await interaction.response.send_message(
            "You don't have permission to use PUG commands.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)
