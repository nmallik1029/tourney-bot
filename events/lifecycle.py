import discord
from discord import app_commands
from core.bot_instance import bot
from core.config import SERVER_ID
from core.storage import tournaments
from views.registration import SignupView, EditRosterView


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        return
    import traceback
    traceback.print_exc()
    print(f"[Error] {error}")


@bot.event
async def on_ready():
    guild = discord.Object(id=SERVER_ID)
    try:
        for t_id, t in tournaments.items():
            if t.get("open"):
                bot.add_view(SignupView(tournament_id=t_id))
            for team in t.get("teams", []):
                bot.add_view(EditRosterView(team_id=team["team_id"], tournament_id=t_id))

        synced = await bot.tree.sync(guild=guild)
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
        print(f"Synced {len(synced)} commands to guild.")
    except Exception as e:
        print(f"Sync failed: {e}")
