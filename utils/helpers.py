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


def _format_ign(ign: str) -> str:
    ign = (ign or "").strip()
    return f" ({ign})" if ign else ""


def build_team_embed(team: dict, tournament_id: str, submitter_name: str) -> discord.Embed:
    players = team["players"]
    captain = players[0]
    embed = discord.Embed(title="New Team Submission", color=0x2B2D31)
    embed.set_author(name=f"Submitted by: {submitter_name}")
    embed.add_field(name="Team Name", value=team["team_name"], inline=False)

    coach = team.get("coach")
    if coach:
        embed.add_field(
            name="Coach",
            value=f"<@{coach['discord_id']}>{_format_ign(coach.get('ign', ''))}",
            inline=False,
        )

    embed.add_field(
        name="Captain",
        value=f"<@{captain['discord_id']}>{_format_ign(captain.get('ign', ''))}",
        inline=False,
    )
    for p in players[1:]:
        embed.add_field(
            name=p["label"],
            value=f"<@{p['discord_id']}>{_format_ign(p.get('ign', ''))}",
            inline=False,
        )

    for sub in team.get("subs") or []:
        embed.add_field(
            name=sub.get("label", "Sub"),
            value=f"<@{sub['discord_id']}>{_format_ign(sub.get('ign', ''))}",
            inline=False,
        )

    embed.set_footer(text=f"Tournament ID: {tournament_id}")
    return embed
