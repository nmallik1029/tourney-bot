import os

import discord
from discord import app_commands

# Branding (shown in embeds/scoreboards). Production server is "CKL".
BRAND = os.environ.get("PUG_BRAND", "CKL")

# PUG admin role, full access, incl. /pug-setup. Set via env or replace fallback.
PUG_ADMIN_ROLE_ID = int(os.environ.get("PUG_ADMIN_ROLE_ID", 0))
# PUG helper role, can use every command EXCEPT /pug-setup and /pug-set-account-link.
PUG_HELPER_ROLE_ID = int(os.environ.get("PUG_HELPER_ROLE_ID", 0))

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


def is_pug_admin():
    """Check that the user has PUG_ADMIN_ROLE_ID or is an administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        if any(r.id == PUG_ADMIN_ROLE_ID for r in interaction.user.roles):
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
    return any(r.id == PUG_ADMIN_ROLE_ID for r in member.roles)


def member_is_pug_staff(member: discord.Member) -> bool:
    """Admin OR helper, used for everything except the two setup commands."""
    if member_is_pug_admin(member):
        return True
    return any(r.id == PUG_HELPER_ROLE_ID for r in member.roles)


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
