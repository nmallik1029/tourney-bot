import re
import discord
from core.storage import load_config


def safe_channel_name(name: str) -> str:
    return re.sub(r"[^a-z0-9\-]", "", name.lower().replace(" ", "-"))


def get_categories(guild: discord.Guild):
    config = load_config()
    t_cat_id = config.get("tournament_category_id")
    a_cat_id = config.get("admin_category_id")
    t_cat = guild.get_channel(t_cat_id) if t_cat_id else None
    a_cat = guild.get_channel(a_cat_id) if a_cat_id else None
    return t_cat, a_cat


def build_team_embed(team: dict, tournament_id: str, submitter_name: str) -> discord.Embed:
    players = team["players"]
    captain = players[0]
    embed = discord.Embed(title="New Team Submission", color=0x2B2D31)
    embed.set_author(name=f"Submitted by: {submitter_name}")
    embed.add_field(name="Team Name", value=team["team_name"], inline=False)
    embed.add_field(
        name="Captain",
        value=f"<@{captain['discord_id']}> ({captain['ign']})",
        inline=False,
    )
    for p in players[1:]:
        embed.add_field(
            name=p["label"],
            value=f"<@{p['discord_id']}> ({p['ign']})",
            inline=False,
        )
    embed.set_footer(text=f"Tournament ID: {tournament_id}")
    return embed
