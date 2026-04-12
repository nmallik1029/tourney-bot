import discord
from discord import app_commands
from core.bot_instance import bot
from core.config import RAILWAY_BASE, is_authorized, guild_object
from core.storage import tournaments, active_matches


@bot.tree.command(
    name="cast",
    description="Toggle casting mode for the match in this channel.",
    guild=guild_object(),
)
@is_authorized()
async def cast_cmd(interaction: discord.Interaction):
    match = next(
        (m for m in active_matches.values() if m.get("channel_id") == interaction.channel.id),
        None,
    )
    if not match:
        await interaction.response.send_message("No active match in this channel.", ephemeral=True)
        return

    match["casted"] = not match.get("casted", False)
    state = "**ON** \U0001f3a5" if match["casted"] else "**OFF**"
    await interaction.response.send_message(
        f"Casting mode is now {state} for this match.\n"
        + ("Game links will be sent to the caster channel first." if match["casted"] else "Game links will go directly to players."),
        ephemeral=True,
    )


@bot.tree.command(
    name="bracket",
    description="Get the admin bracket panel link to start matches.",
    guild=guild_object(),
)
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID")
async def bracket_cmd(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    token = t.get("admin_token")
    if not token:
        await interaction.response.send_message("No admin panel available for this tournament.", ephemeral=True)
        return

    admin_url = f"{RAILWAY_BASE}/admin/{t_id}?token={token}"
    await interaction.response.send_message(
        f"**Admin Bracket Panel:** {admin_url}\n"
        f"**Challonge:** {t.get('challonge_url', 'N/A')}\n\n"
        f"Click matches on the admin panel to start them.",
        ephemeral=True,
    )
