import discord
from discord import app_commands
from core.bot_instance import bot
from core.config import SERVER_ID, GUILD_IDS
from core.guild_ctx import set_guild
from core.storage import tournaments
from views.registration import SignupView, EditRosterView
from views.vod import VODSubmissionView


async def _bind_guild_for_command(interaction: discord.Interaction) -> bool:
    """Bind the per-guild context for every slash command before it runs. Component
    (button/select/modal) interactions are handled by GuildView/GuildModal instead."""
    if interaction.guild_id is not None:
        set_guild(interaction.guild_id)
    return True


# Replace the tree's default (always-True) check so guild context is set globally.
bot.tree.interaction_check = _bind_guild_for_command


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        return
    import traceback
    traceback.print_exc()
    print(f"[Error] {error}")


@bot.event
async def on_ready():
    try:
        # One persistent EditRosterView handles every team embed -- the button
        # callback resolves the correct team by looking up the clicked message's ID.
        bot.add_view(EditRosterView())

        for t_id, t in tournaments.items():
            if t.get("open"):
                bot.add_view(SignupView(tournament_id=t_id))

            # Re-register VOD submission views for teams awaiting VODs
            for team_id, vod_info in t.get("vod_teams", {}).items():
                team = next((tm for tm in t["teams"] if tm["team_id"] == team_id), None)
                if team:
                    bot.add_view(VODSubmissionView(
                        tournament_id=t_id,
                        team_id=team_id,
                        players=team.get("players", []),
                    ))

        # Commands are defined globally; copy them into each authorized guild and sync
        # per-guild (instant propagation). Add a server to GUILD_IDS to authorize it.
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
        for gid in GUILD_IDS:
            guild = discord.Object(id=gid)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {gid}.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Sync failed: {e}")
