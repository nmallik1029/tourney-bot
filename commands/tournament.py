import uuid
import os
import discord
from discord import app_commands
from core.bot_instance import bot
from core.config import SERVER_ID, ROLE_ID, RAILWAY_BASE, is_authorized, guild_object
from core.storage import tournaments, save_tournaments, save_config, active_matches, _dashboard_tokens
from views.registration import SignupView


# ── /tournament-setup ──────────────────────────────────────────────────────────
class TournamentCategorySelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="Tournament channels (signups, updates, matches, pick/ban)...",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.tournament_category_id = int(self.values[0])
        await interaction.response.defer()


class AdminCategorySelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="Admin channels (tournament-admin)...",
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.admin_category_id = int(self.values[0])
        await interaction.response.defer()


class SetupView(discord.ui.View):
    def __init__(self, options):
        super().__init__(timeout=120)
        self.tournament_category_id = None
        self.admin_category_id = None
        self.add_item(TournamentCategorySelect(options))
        self.add_item(AdminCategorySelect(options))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.tournament_category_id or not self.admin_category_id:
            await interaction.response.send_message("Please select both categories before saving.", ephemeral=True)
            return

        save_config({
            "tournament_category_id": self.tournament_category_id,
            "admin_category_id": self.admin_category_id,
        })

        t_cat = interaction.guild.get_channel(self.tournament_category_id)
        a_cat = interaction.guild.get_channel(self.admin_category_id)

        await interaction.response.send_message(
            f"Setup saved!\n"
            f"Tournament channels -> **{t_cat.name if t_cat else 'Unknown'}**\n"
            f"Admin channels -> **{a_cat.name if a_cat else 'Unknown'}**",
            ephemeral=True,
        )


@bot.tree.command(
    name="tournament-setup",
    description="Configure which categories tournament channels are placed in.",
    guild=guild_object(),
)
@is_authorized()
async def tournament_setup(interaction: discord.Interaction):
    categories = interaction.guild.categories
    if not categories:
        await interaction.response.send_message("No categories found in this server.", ephemeral=True)
        return

    options = [
        discord.SelectOption(label=cat.name[:100], value=str(cat.id))
        for cat in categories[:25]
    ]

    view = SetupView(options=options)
    await interaction.response.send_message(
        "**Tournament Setup**\nSelect which category each group of channels should go in:",
        view=view,
        ephemeral=True,
    )


# ── /tournament-create ──────────────────────────────────────────────────────────
class TournamentCreateModal(discord.ui.Modal, title="Create Tournament"):
    tournament_name = discord.ui.TextInput(
        label="Tournament Name",
        placeholder="e.g. FRVR X NACK $700 4v4 Tournament",
        max_length=100,
    )
    format = discord.ui.TextInput(
        label="Format",
        placeholder="1v1 | 2v2 | 3v3 | 4v4",
        max_length=10,
    )
    prize_pool = discord.ui.TextInput(
        label="Prize Pool (1st / 2nd / 3rd)",
        placeholder="e.g. 420 / 210 / 70",
        max_length=50,
    )
    date = discord.ui.TextInput(
        label="Date (Discord Timestamp)",
        placeholder="e.g. <t:1743188400:F>  -- use discordtimestamp.com",
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction):
        from utils.helpers import get_categories

        guild = interaction.guild
        fmt = self.format.value.strip().lower()
        valid_formats = {"1v1": 1, "2v2": 2, "3v3": 3, "4v4": 4}
        if fmt not in valid_formats:
            await interaction.response.send_message(
                "Invalid format. Must be one of: `1v1`, `2v2`, `3v3`, `4v4`.",
                ephemeral=True,
            )
            return

        team_size = valid_formats[fmt]
        tournament_id = str(uuid.uuid4())[:8].upper()
        raw_prizes = self.prize_pool.value.strip()
        prize_parts = [p.strip().lstrip("$") for p in raw_prizes.split("/")]
        prize_display = " / ".join(f"${p}" for p in prize_parts)

        t_cat, _ = get_categories(guild)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, view_channel=True),
            guild.me: discord.PermissionOverwrite(send_messages=True, manage_channels=True),
        }
        signups_channel = await guild.create_text_channel(
            name="signups",
            overwrites=overwrites,
            topic=f"Tournament registration -- ID: {tournament_id}",
            category=t_cat,
        )

        embed = discord.Embed(
            title=f"{self.tournament_name.value}  --  Registration",
            description=f"Click the button below to register for the **{self.tournament_name.value}**!",
            color=0x00FF7F,
        )
        embed.add_field(name="Prizes", value=prize_display, inline=True)
        embed.add_field(name="Format", value=self.format.value.upper(), inline=True)
        embed.add_field(name="Date", value=self.date.value, inline=True)
        embed.set_footer(text=f"Tournament ID: {tournament_id}")

        tournaments[tournament_id] = {
            "id": tournament_id,
            "name": self.tournament_name.value,
            "format": fmt,
            "team_size": team_size,
            "prizes": prize_display,
            "date": self.date.value,
            "signups_channel_id": signups_channel.id,
            "organizer_id": interaction.user.id,
            "organizer_name": interaction.user.display_name,
            "teams": [],
            "open": True,
        }

        view = SignupView(tournament_id=tournament_id)
        save_tournaments()
        signup_btn_msg = await signups_channel.send(embed=embed, view=view)
        tournaments[tournament_id]["signup_button_message_id"] = signup_btn_msg.id
        save_tournaments()
        await interaction.response.send_message(
            f"Tournament **{self.tournament_name.value}** created!\n"
            f"Sign-ups are open in {signups_channel.mention}\n"
            f"Tournament ID: `{tournament_id}`",
            ephemeral=True,
        )


@bot.tree.command(name="tournament-create", description="Create a new tournament and open sign-ups.", guild=guild_object())
@is_authorized()
async def tournament_create(interaction: discord.Interaction):
    await interaction.response.send_modal(TournamentCreateModal())


# ── /tournament-start ──────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-start", description="Close sign-ups and start the tournament.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID from /tournament-create")
async def tournament_start(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"] and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the organizer or an admin can start this tournament.", ephemeral=True)
        return

    if not t["open"]:
        await interaction.response.send_message("This tournament has already started.", ephemeral=True)
        return

    if not t.get("admin_token"):
        t["admin_token"] = str(uuid.uuid4())
        save_tournaments()

    seeding_url = f"{RAILWAY_BASE}/admin/{t_id}/seeding?token={t['admin_token']}"
    await interaction.response.send_message(
        f"**Open the seeding page to reorder teams and start the bracket:**\n{seeding_url}",
        ephemeral=True,
    )


# ── /tournament-info ──────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-info", description="Open the tournament dashboard.", guild=guild_object())
@is_authorized()
async def tournament_info(interaction: discord.Interaction):
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if not dashboard_token:
        dashboard_token = str(uuid.uuid4())
        _dashboard_tokens.add(dashboard_token)

    url = f"{RAILWAY_BASE}/dashboard?token={dashboard_token}"
    await interaction.response.send_message(f"**Tournament Dashboard:**\n{url}", ephemeral=True)


# ── /tournament-remove-team ───────────────────────────────────────────────────
@bot.tree.command(name="tournament-remove-team", description="Remove a team from a tournament.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID", team_name="The team name to remove")
async def tournament_remove_team(interaction: discord.Interaction, tournament_id: str, team_name: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    found_team = None
    for tm in t.get("teams", []):
        if tm["team_name"].lower() == team_name.lower():
            found_team = tm
            break

    if not found_team:
        team_list = ", ".join(tm["team_name"] for tm in t.get("teams", []))
        await interaction.response.send_message(
            f"Team `{team_name}` not found.\nRegistered teams: {team_list or 'None'}",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    signups_ch = interaction.guild.get_channel(t.get("signups_channel_id"))
    if signups_ch and found_team.get("signup_message_id"):
        try:
            msg = await signups_ch.fetch_message(found_team["signup_message_id"])
            await msg.delete()
        except Exception:
            pass

    t["teams"].remove(found_team)
    save_tournaments()

    await interaction.followup.send(f"Team **{found_team['team_name']}** has been removed from `{t_id}`.")


# ── /tournament-end ───────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-end", description="End a tournament -- clean up channels/roles but keep results.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID to end")
async def tournament_end(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"] and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the organizer or an admin can end this tournament.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Delete match room channels from active_matches
    match_ids_to_remove = []
    for mid, match in active_matches.items():
        if match.get("tournament_id") == t_id:
            ch_id = match.get("channel_id")
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
            match_ids_to_remove.append(mid)
    for mid in match_ids_to_remove:
        del active_matches[mid]

    # Also delete any channels in the Matchrooms category
    matchrooms_cat = discord.utils.get(guild.categories, name="Matchrooms")
    if matchrooms_cat:
        for ch in matchrooms_cat.channels:
            try:
                await ch.delete()
            except Exception:
                pass
        try:
            await matchrooms_cat.delete()
        except Exception:
            pass

    # Delete tournament channels
    for key in ["signups_channel_id", "updates_channel_id", "admin_channel_id", "caster_channel_id"]:
        ch_id = t.get(key)
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.delete()
                except Exception:
                    pass

    # Delete per-team channels, categories, and roles
    for team_id, ch_ids in t.get("team_channels", {}).items():
        for ch_id in [ch_ids.get("text"), ch_ids.get("voice")]:
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
    for team_id, cat_id in t.get("team_categories", {}).items():
        cat = guild.get_channel(cat_id)
        if cat:
            try:
                await cat.delete()
            except Exception:
                pass
    for team_id, role_id in t.get("team_roles", {}).items():
        role = guild.get_role(role_id)
        if role:
            try:
                await role.delete()
            except Exception:
                pass

    t["open"] = False
    t["ended"] = True
    t.pop("signups_channel_id", None)
    t.pop("updates_channel_id", None)
    t.pop("admin_channel_id", None)
    t.pop("team_channels", None)
    t.pop("team_categories", None)
    t.pop("team_roles", None)
    save_tournaments()

    await interaction.followup.send(
        f"Tournament `{t_id}` has ended. All channels and roles cleaned up.\n"
        f"Results are preserved -- view them on the dashboard or Challonge: {t.get('challonge_url', 'N/A')}"
    )


# ── /tournament-delete ─────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-delete", description="Delete a tournament and its channels.", guild=guild_object())
@is_authorized()
@app_commands.describe(tournament_id="The tournament ID to delete")
async def tournament_delete(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"] and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only the organizer or an admin can delete this tournament.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    for key in ["signups_channel_id", "updates_channel_id", "matches_channel_id", "admin_channel_id", "bracket_channel_id", "caster_channel_id"]:
        ch_id = t.get(key)
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.delete()
                except Exception:
                    pass

    match_ids_to_remove = []
    for mid, match in active_matches.items():
        if match.get("tournament_id") == t_id:
            ch_id = match.get("channel_id")
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
            match_ids_to_remove.append(mid)
    for mid in match_ids_to_remove:
        del active_matches[mid]

    for team_id, ch_ids in t.get("team_channels", {}).items():
        for ch_id in [ch_ids.get("text"), ch_ids.get("voice")]:
            if ch_id:
                ch = guild.get_channel(ch_id)
                if ch:
                    try:
                        await ch.delete()
                    except Exception:
                        pass
    for team_id, cat_id in t.get("team_categories", {}).items():
        cat = guild.get_channel(cat_id)
        if cat:
            try:
                await cat.delete()
            except Exception:
                pass
    for team_id, role_id in t.get("team_roles", {}).items():
        role = guild.get_role(role_id)
        if role:
            try:
                await role.delete()
            except Exception:
                pass

    del tournaments[t_id]
    save_tournaments()
    await interaction.followup.send(f"Tournament `{t_id}` has been deleted.")
