import discord
from discord import app_commands
from discord.ext import commands
import uuid
import json
import re
import asyncio
import urllib.parse
from pathlib import Path
from aiohttp import web

# ── Bot setup ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Persistent storage ─────────────────────────────────────────────────────────
TOURNAMENTS_FILE = Path("tournaments.json")

def load_tournaments() -> dict:
    if TOURNAMENTS_FILE.exists():
        with open(TOURNAMENTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_tournaments():
    with open(TOURNAMENTS_FILE, "w") as f:
        json.dump(tournaments, f, indent=2, default=str)

def load_config() -> dict:
    config_file = Path("config.json")
    if config_file.exists():
        with open(config_file, "r") as f:
            return json.load(f)
    return {}

def save_config(data: dict):
    with open("config.json", "w") as f:
        json.dump(data, f, indent=2)

tournaments: dict[str, dict] = load_tournaments()

STAFF_ROLE_ID = 1489047073142739035
SERVER_ID = 1489359260348579840

SCORES = {
    "bo1": ["1-0"],
    "bo3": ["2-0", "2-1"],
    "bo5": ["3-0", "3-1", "3-2"],
}

pending_registrations: dict[int, dict] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────
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


def find_tournament_by_matches_channel(channel_id: int):
    for t_id, t in tournaments.items():
        if t.get("matches_channel_id") == channel_id:
            return t_id, t
    return None, None


def team_label(team: dict) -> str:
    captain = team["players"][0]
    return f"{team['team_name']} (cap: {captain['ign']})"


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
            f"Tournament channels → **{t_cat.name if t_cat else 'Unknown'}**\n"
            f"Admin channels → **{a_cat.name if a_cat else 'Unknown'}**",
            ephemeral=True,
        )


@bot.tree.command(
    name="tournament-setup",
    description="Configure which categories tournament channels are placed in.",
    guild=discord.Object(id=SERVER_ID),
)
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


# ── Modal: /tournament-create ──────────────────────────────────────────────────
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
        placeholder="e.g. <t:1743188400:F>  — use discordtimestamp.com",
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction):
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
            topic=f"Tournament registration — ID: {tournament_id}",
            category=t_cat,
        )

        embed = discord.Embed(
            title=f"{self.tournament_name.value}  —  Registration",
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
            "teams": [],
            "open": True,
        }

        view = SignupView(tournament_id=tournament_id)
        save_tournaments()
        await signups_channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"Tournament **{self.tournament_name.value}** created!\n"
            f"Sign-ups are open in {signups_channel.mention}\n"
            f"Tournament ID: `{tournament_id}`",
            ephemeral=True,
        )


# ── Register Your Team button ──────────────────────────────────────────────────
class SignupView(discord.ui.View):
    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id

    @discord.ui.button(label="Register Your Team", style=discord.ButtonStyle.success, custom_id="register_team_btn")
    async def register_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        t_id = self.tournament_id
        if t_id not in tournaments:
            topic = interaction.channel.topic or ""
            for tid, t in tournaments.items():
                if tid in topic:
                    t_id = tid
                    break
            else:
                await interaction.response.send_message("Tournament not found. It may have already closed.", ephemeral=True)
                return

        t = tournaments[t_id]
        if not t["open"]:
            await interaction.response.send_message("Sign-ups for this tournament are closed.", ephemeral=True)
            return

        team_size = t["team_size"]
        view = PlayerSelectView(tournament_id=t_id, team_size=team_size)
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        instructions = "\n".join(f"**Slot {i+1} — {label}**" for i, label in enumerate(player_labels))

        await interaction.response.send_message(
            f"**Select your team members:**\n{instructions}\n\nUse the menus below to select each player, then click Submit.",
            view=view,
            ephemeral=True,
        )


# ── Step 1: Player user-select menus ──────────────────────────────────────────
class PlayerSelectView(discord.ui.View):
    def __init__(self, tournament_id: str, team_size: int):
        super().__init__(timeout=300)
        self.tournament_id = tournament_id
        self.team_size = team_size
        self.selected_users: dict[int, discord.Member] = {}

        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels):
            self.add_item(PlayerUserSelect(slot_index=i, label=label))

    @discord.ui.button(label="Next: Enter IGNs", style=discord.ButtonStyle.primary, row=4)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.selected_users) < self.team_size:
            await interaction.response.send_message(
                f"Please select all {self.team_size} players before continuing.",
                ephemeral=True,
            )
            return

        pending_registrations[interaction.user.id] = {
            "tournament_id": self.tournament_id,
            "selected_users": {i: m for i, m in self.selected_users.items()},
        }

        modal = IGNModal(
            tournament_id=self.tournament_id,
            selected_users=self.selected_users,
            team_size=self.team_size,
        )
        await interaction.response.send_modal(modal)


class PlayerUserSelect(discord.ui.UserSelect):
    def __init__(self, slot_index: int, label: str):
        super().__init__(
            placeholder=f"Select {label}...",
            row=min(slot_index, 3),
        )
        self.slot_index = slot_index

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_users[self.slot_index] = self.values[0]
        await interaction.response.defer()


# ── Step 2: IGN modal ──────────────────────────────────────────────────────────
class IGNModal(discord.ui.Modal, title="Enter Team Name & IGNs"):
    def __init__(self, tournament_id: str, selected_users: dict, team_size: int):
        super().__init__()
        self.tournament_id = tournament_id
        self.selected_users = selected_users
        self.team_size = team_size

        self.team_name_field = discord.ui.TextInput(
            label="Team Name",
            placeholder="Enter your team name",
            max_length=50,
        )
        self.add_item(self.team_name_field)

        self.ign_fields = []
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels[:4]):
            field = discord.ui.TextInput(
                label=f"{label} IGN",
                placeholder=f"In-game name for {selected_users.get(i, 'this player')}",
                max_length=32,
            )
            self.add_item(field)
            self.ign_fields.append((i, label, field))

    async def on_submit(self, interaction: discord.Interaction):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        team_name = self.team_name_field.value.strip()
        players = []

        for i, label, field in self.ign_fields:
            member = self.selected_users.get(i)
            if not member:
                continue
            players.append({
                "label": label,
                "discord_id": member.id,
                "ign": field.value.strip(),
            })

        team_entry = {
            "team_id": str(uuid.uuid4()),
            "team_name": team_name,
            "submitted_by": interaction.user.id,
            "submitter_name": interaction.user.name,
            "signup_message_id": None,
            "players": players,
            "submitted_at": str(discord.utils.utcnow()),
        }
        tournaments[self.tournament_id]["teams"].append(team_entry)
        save_tournaments()

        signups_ch = interaction.guild.get_channel(t["signups_channel_id"])
        if signups_ch:
            embed = build_team_embed(team_entry, self.tournament_id, interaction.user.name)
            view = EditRosterView(team_id=team_entry["team_id"], tournament_id=self.tournament_id)
            msg = await signups_ch.send(embed=embed, view=view)
            team_entry["signup_message_id"] = msg.id
            save_tournaments()

        await interaction.response.send_message(
            f"**Team '{team_name}' registered!** You'll be notified when the tournament starts.",
            ephemeral=True,
        )


# ── Edit Roster ────────────────────────────────────────────────────────────────
class EditRosterView(discord.ui.View):
    def __init__(self, team_id: str, tournament_id: str):
        super().__init__(timeout=None)
        self.team_id = team_id
        self.tournament_id = tournament_id

    @discord.ui.button(label="Edit Roster", style=discord.ButtonStyle.primary, custom_id="edit_roster_btn")
    async def edit_roster(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("Team not found.", ephemeral=True)
            return

        if interaction.user.id != team["submitted_by"]:
            await interaction.response.send_message("Only the person who registered this team can edit it.", ephemeral=True)
            return

        if not t["open"]:
            await interaction.response.send_message("Sign-ups are closed. The roster can no longer be edited.", ephemeral=True)
            return

        view = EditPlayerSelectView(
            tournament_id=self.tournament_id,
            team_id=self.team_id,
            team_size=t["team_size"],
        )
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, t["team_size"] + 1)]
        instructions = "\n".join(f"**Slot {i+1} — {label}**" for i, label in enumerate(player_labels))

        await interaction.response.send_message(
            f"**Update your team members:**\n{instructions}\n\nSelect new players and click Next.",
            view=view,
            ephemeral=True,
        )


class EditPlayerSelectView(discord.ui.View):
    def __init__(self, tournament_id: str, team_id: str, team_size: int):
        super().__init__(timeout=300)
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.team_size = team_size
        self.selected_users: dict[int, discord.Member] = {}

        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels):
            self.add_item(PlayerUserSelect(slot_index=i, label=label))

    @discord.ui.button(label="Next: Update IGNs", style=discord.ButtonStyle.primary, row=4)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.selected_users) < self.team_size:
            await interaction.response.send_message(
                f"Please select all {self.team_size} players before continuing.",
                ephemeral=True,
            )
            return

        modal = EditIGNModal(
            tournament_id=self.tournament_id,
            team_id=self.team_id,
            selected_users=self.selected_users,
            team_size=self.team_size,
        )
        await interaction.response.send_modal(modal)


class EditIGNModal(discord.ui.Modal, title="Update Team Name & IGNs"):
    def __init__(self, tournament_id: str, team_id: str, selected_users: dict, team_size: int):
        super().__init__()
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.selected_users = selected_users
        self.team_size = team_size

        t = tournaments.get(tournament_id, {})
        team = next((tm for tm in t.get("teams", []) if tm["team_id"] == team_id), {})

        self.team_name_field = discord.ui.TextInput(
            label="Team Name",
            placeholder="Enter new team name",
            default=team.get("team_name", ""),
            max_length=50,
        )
        self.add_item(self.team_name_field)

        self.ign_fields = []
        player_labels = ["Captain"] + [f"Player {i}" for i in range(2, team_size + 1)]
        for i, label in enumerate(player_labels[:4]):
            existing_ign = ""
            if team.get("players") and i < len(team["players"]):
                existing_ign = team["players"][i].get("ign", "")
            field = discord.ui.TextInput(
                label=f"{label} IGN",
                placeholder=f"IGN for {selected_users.get(i, 'this player')}",
                default=existing_ign,
                max_length=32,
            )
            self.add_item(field)
            self.ign_fields.append((i, label, field))

    async def on_submit(self, interaction: discord.Interaction):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("Team not found.", ephemeral=True)
            return

        team["team_name"] = self.team_name_field.value.strip()
        new_players = []
        for i, label, field in self.ign_fields:
            member = self.selected_users.get(i)
            if not member:
                continue
            new_players.append({
                "label": label,
                "discord_id": member.id,
                "ign": field.value.strip(),
            })

        team["players"] = new_players
        save_tournaments()

        signups_ch = interaction.guild.get_channel(t["signups_channel_id"])
        if signups_ch and team.get("signup_message_id"):
            try:
                original_msg = await signups_ch.fetch_message(team["signup_message_id"])
                updated_embed = build_team_embed(team, self.tournament_id, team["submitter_name"])
                view = EditRosterView(team_id=self.team_id, tournament_id=self.tournament_id)
                await original_msg.edit(embed=updated_embed, view=view)
            except discord.NotFound:
                pass

        await interaction.response.send_message(
            f"Roster for **{team['team_name']}** updated successfully!",
            ephemeral=True,
        )


# ── Match result: Step 1 — BO select ──────────────────────────────────────────
class BoSelectView(discord.ui.View):
    def __init__(self, tournament_id: str, original_message_id: int, matches_channel_id: int, image_url: str, submitter_name: str):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id
        self.original_message_id = original_message_id
        self.matches_channel_id = matches_channel_id
        self.image_url = image_url
        self.submitter_name = submitter_name

    @discord.ui.select(
        placeholder="Select Best of...",
        custom_id="bo_select",
        options=[
            discord.SelectOption(label="BO1 — Best of 1", value="bo1"),
            discord.SelectOption(label="BO3 — Best of 3", value="bo3"),
            discord.SelectOption(label="BO5 — Best of 5", value="bo5"),
        ],
    )
    async def bo_selected(self, interaction: discord.Interaction, select: discord.ui.Select):
        bo = select.values[0]
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        teams = t.get("teams", [])
        if len(teams) < 2:
            await interaction.response.send_message("Not enough teams registered.", ephemeral=True)
            return

        team_options = [
            discord.SelectOption(label=team_label(tm)[:100], value=tm["team_id"])
            for tm in teams[:25]
        ]
        score_options = [
            discord.SelectOption(label=score, value=score)
            for score in SCORES[bo]
        ]

        new_view = ResultDetailsView(
            tournament_id=self.tournament_id,
            original_message_id=self.original_message_id,
            matches_channel_id=self.matches_channel_id,
            image_url=self.image_url,
            submitter_name=self.submitter_name,
            bo=bo,
            team_options=team_options,
            score_options=score_options,
        )

        await interaction.response.edit_message(
            content=f"**BO selected: {bo.upper()}**\nNow select the winner, loser, and score.",
            view=new_view,
        )


# ── Match result: Step 2 — Winner / Loser / Score ─────────────────────────────
class ResultDetailsView(discord.ui.View):
    def __init__(self, tournament_id, original_message_id, matches_channel_id, image_url, submitter_name, bo, team_options, score_options):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id
        self.original_message_id = original_message_id
        self.matches_channel_id = matches_channel_id
        self.image_url = image_url
        self.submitter_name = submitter_name
        self.bo = bo
        self.selected_winner = None
        self.selected_loser = None
        self.selected_score = None

        self.add_item(WinnerSelect(team_options))
        self.add_item(LoserSelect(team_options))
        self.add_item(ScoreSelect(score_options))

    @discord.ui.button(label="Submit Result", style=discord.ButtonStyle.success, custom_id="submit_result_btn", row=4)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_winner or not self.selected_loser or not self.selected_score:
            await interaction.response.send_message("Please select winner, loser, and score before submitting.", ephemeral=True)
            return

        if self.selected_winner == self.selected_loser:
            await interaction.response.send_message("Winner and loser cannot be the same team.", ephemeral=True)
            return

        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        winner_team = next((tm for tm in t["teams"] if tm["team_id"] == self.selected_winner), None)
        loser_team = next((tm for tm in t["teams"] if tm["team_id"] == self.selected_loser), None)

        if not winner_team or not loser_team:
            await interaction.response.send_message("Could not find selected teams.", ephemeral=True)
            return

        updates_channel = interaction.guild.get_channel(t["updates_channel_id"])
        matches_channel = interaction.guild.get_channel(self.matches_channel_id)

        if updates_channel:
            embed = discord.Embed(
                description=f"## **{winner_team['team_name']}** won {self.selected_score} against **{loser_team['team_name']}**",
                color=0x2B2D31,
            )
            embed.set_image(url=self.image_url)
            await updates_channel.send(embed=embed)

        if matches_channel:
            try:
                original = await matches_channel.fetch_message(self.original_message_id)
                await original.add_reaction("✅")
            except discord.NotFound:
                pass

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"Result approved: **{winner_team['team_name']}** won {self.selected_score} against **{loser_team['team_name']}**",
            view=self,
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="deny_result_btn", row=4)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        matches_channel = interaction.guild.get_channel(self.matches_channel_id)
        if matches_channel:
            try:
                original = await matches_channel.fetch_message(self.original_message_id)
                await original.add_reaction("❌")
            except discord.NotFound:
                pass

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Result denied.", view=self)


class WinnerSelect(discord.ui.Select):
    def __init__(self, team_options):
        super().__init__(placeholder="Select winner...", custom_id="winner_select", options=team_options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_winner = self.values[0]
        await interaction.response.defer()


class LoserSelect(discord.ui.Select):
    def __init__(self, team_options):
        super().__init__(placeholder="Select loser...", custom_id="loser_select", options=team_options, row=2)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_loser = self.values[0]
        await interaction.response.defer()


class ScoreSelect(discord.ui.Select):
    def __init__(self, score_options):
        super().__init__(placeholder="Select score...", custom_id="score_select", options=score_options, row=3)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_score = self.values[0]
        await interaction.response.defer()


# ── on_message: forward image to admin ────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    t_id, t = find_tournament_by_matches_channel(message.channel.id)
    if t is None:
        return

    if not message.attachments:
        return

    admin_channel = message.guild.get_channel(t["admin_channel_id"])
    if not admin_channel:
        return

    attachment = message.attachments[0]
    embed = discord.Embed(title="Match Result Submission", color=0x2B2D31)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.set_image(url=attachment.url)
    embed.set_footer(text=f"Tournament: {t['name']}  |  Message ID: {message.id}")

    view = BoSelectView(
        tournament_id=t_id,
        original_message_id=message.id,
        matches_channel_id=message.channel.id,
        image_url=attachment.url,
        submitter_name=message.author.display_name,
    )
    await admin_channel.send(content="Select the Best of format to continue:", embed=embed, view=view)


# ── /tournament-start ──────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-start", description="Close sign-ups and start the tournament.", guild=discord.Object(id=SERVER_ID))
@app_commands.describe(tournament_id="The tournament ID from /tournament-create")
async def tournament_start(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"]:
        await interaction.response.send_message("Only the tournament organizer can start this tournament.", ephemeral=True)
        return

    if not t["open"]:
        await interaction.response.send_message("This tournament has already started.", ephemeral=True)
        return

    t["open"] = False
    team_count = len(t["teams"])
    guild = interaction.guild
    staff_role = guild.get_role(STAFF_ROLE_ID)
    t_cat, a_cat = get_categories(guild)

    read_only = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
    }
    matches_perms = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=False),
        guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True, add_reactions=True),
    }
    admin_perms = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        admin_perms[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    updates_channel = await guild.create_text_channel(name="tournament-updates", overwrites=read_only, category=t_cat)
    matches_channel = await guild.create_text_channel(name="tournament-matches", overwrites=matches_perms, slowmode_delay=600, category=t_cat)
    admin_channel = await guild.create_text_channel(name="tournament-admin", overwrites=admin_perms, category=a_cat)

    t["updates_channel_id"] = updates_channel.id
    t["matches_channel_id"] = matches_channel.id
    t["admin_channel_id"] = admin_channel.id
    save_tournaments()

    signups_channel = guild.get_channel(t["signups_channel_id"])
    if signups_channel:
        await signups_channel.send(f"Sign-ups closed — {team_count} team{'s' if team_count != 1 else ''} registered.")

    await updates_channel.send(
        f"**{t['name']}** has started with {team_count} team{'s' if team_count != 1 else ''}.\nBracket coming soon."
    )

    await interaction.response.send_message(
        f"Tournament started.\nUpdates: {updates_channel.mention}\nMatches: {matches_channel.mention}\nAdmin: {admin_channel.mention}",
        ephemeral=True,
    )


# ── /tournament-create ─────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-create", description="Create a new tournament and open sign-ups.", guild=discord.Object(id=SERVER_ID))
async def tournament_create(interaction: discord.Interaction):
    await interaction.response.send_modal(TournamentCreateModal())


# ── /tournament-delete ─────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-delete", description="Delete a tournament and its channels.", guild=discord.Object(id=SERVER_ID))
@app_commands.describe(tournament_id="The tournament ID to delete")
async def tournament_delete(interaction: discord.Interaction, tournament_id: str):
    t_id = tournament_id.upper()
    if t_id not in tournaments:
        await interaction.response.send_message(f"Tournament `{t_id}` not found.", ephemeral=True)
        return

    t = tournaments[t_id]
    if interaction.user.id != t["organizer_id"]:
        await interaction.response.send_message("Only the tournament organizer can delete this tournament.", ephemeral=True)
        return

    guild = interaction.guild
    for key in ["signups_channel_id", "updates_channel_id", "matches_channel_id", "admin_channel_id", "bracket_channel_id"]:
        ch_id = t.get(key)
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                await ch.delete()

    del tournaments[t_id]
    save_tournaments()
    await interaction.response.send_message(f"Tournament `{t_id}` has been deleted.", ephemeral=True)


# ── Bot ready ──────────────────────────────────────────────────────────────────
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


# ── Webhook server ─────────────────────────────────────────────────────────────
WEBHOOK_PORT = 5000


async def handle_krunker_webhook(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    event_type = payload.get("type")
    if event_type != "match_end":
        return web.Response(status=200, text="ok")

    teams = payload.get("teams", [])
    players = payload.get("players", [])
    winner_team_num = payload.get("winner")
    map_name = payload.get("map", "Unknown")

    if len(teams) < 2 or winner_team_num is None:
        return web.Response(status=200, text="ok")

    team1_name = teams[0]["name"]
    team2_name = teams[1]["name"]

    matched_tournament = None
    matched_team1 = None
    matched_team2 = None

    for t_id, t in tournaments.items():
        if t.get("open"):
            continue
        if not t.get("updates_channel_id"):
            continue

        registered_teams = t.get("teams", [])
        t1 = next((tm for tm in registered_teams if tm["team_name"].strip().lower() == team1_name.strip().lower()), None)
        t2 = next((tm for tm in registered_teams if tm["team_name"].strip().lower() == team2_name.strip().lower()), None)

        if t1 and t2:
            matched_tournament = t
            matched_team1 = t1
            matched_team2 = t2
            break

    if not matched_tournament:
        print(f"[Webhook] No tournament match found for teams: {team1_name} vs {team2_name}")
        return web.Response(status=200, text="ok")

    winner_krunker = next((tm for tm in teams if tm["team"] == winner_team_num), None)
    loser_krunker = next((tm for tm in teams if tm["team"] != winner_team_num), None)

    if not winner_krunker or not loser_krunker:
        return web.Response(status=200, text="ok")

    winner_discord = matched_team1 if matched_team1["team_name"].strip().lower() == winner_krunker["name"].strip().lower() else matched_team2
    loser_discord = matched_team1 if winner_discord == matched_team2 else matched_team2

    winner_score = winner_krunker["score"]
    loser_score = loser_krunker["score"]
    score_str = f"{winner_score}-{loser_score}"

    embed = discord.Embed(
        description=f"## **{winner_discord['team_name']}** won {score_str} against **{loser_discord['team_name']}**",
        color=0x2B2D31,
    )
    embed.add_field(name="Map", value=map_name, inline=True)

    winner_players = [p for p in players if p["team"] == winner_team_num]
    loser_players = [p for p in players if p["team"] != winner_team_num]

    def player_stats_text(player_list):
        lines = []
        for p in player_list:
            lines.append(f"`{p['name']}` — {p['kills']}K / {p['deaths']}D  •  {p['accuracy']}% acc")
        return "\n".join(lines) if lines else "No data"

    embed.add_field(name=f"{winner_discord['team_name']} (W)", value=player_stats_text(winner_players), inline=False)
    embed.add_field(name=f"{loser_discord['team_name']} (L)", value=player_stats_text(loser_players), inline=False)

    for g in bot.guilds:
        ch = g.get_channel(matched_tournament["updates_channel_id"])
        if ch:
            await ch.send(embed=embed)
            print(f"[Webhook] Posted result: {winner_discord['team_name']} won {score_str} vs {loser_discord['team_name']}")
            break

    return web.Response(status=200, text="ok")


async def handle_launch(request: web.Request) -> web.Response:
    client = request.query.get("client", "glorp")
    params = {k: v for k, v in request.query.items() if k != "client"}
    query = urllib.parse.urlencode(params)
    if client == "crankshaft":
        target_url = f"crankshaft://game?{query}"
    else:
        target_url = f"glorp://game?{query}"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>Launching {client.title()}...</title>
  <style>
    body {{ font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #1e1f22; color: #fff; }}
    p {{ font-size: 1.2rem; }}
  </style>
</head>
<body>
  <p>Launching {client.title()}... You can close this tab.</p>
  <script>window.location = "{target_url}";</script>
</body>
</html>"""
    return web.Response(content_type="text/html", text=html)


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/krunker", handle_krunker_webhook)
    app.router.add_get("/launch", handle_launch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"Webhook server running on port {WEBHOOK_PORT}")


# ── Pick/Ban system ────────────────────────────────────────────────────────────
MAPS = ["Bureau", "Lush", "Site", "Industry", "Undergrowth", "Sandstorm", "Burg"]

MAP_IDS = {
    "Bureau": "Bureau",
    "Lush": "Lush",
    "Site": "Site",
    "Industry": "Industry",
    "Undergrowth": "Undergrowth",
    "Sandstorm": "Sandstorm",
    "Burg": "Burg",
}

WEBHOOK_URL = "https://tourney-bot-production.up.railway.app/krunker"

PB_SEQUENCES = {
    "bo1": [
        (0, "ban"), (1, "ban"), (0, "ban"), (1, "ban"), (0, "ban"), (1, "ban"),
    ],
    "bo3": [
        (0, "ban"), (1, "ban"),
        (0, "pick"), (1, "pick"),
        (0, "ban"), (1, "ban"),
    ],
    "bo5": [
        (0, "ban"), (0, "ban"),
        (0, "pick"), (1, "pick"), (0, "pick"), (1, "pick"),
    ],
}

active_matches: dict[int, dict] = {}


def get_current_step(match: dict):
    seq = PB_SEQUENCES[match["format"]]
    step = match["step"]
    if step < len(seq):
        return seq[step]
    return None


def build_pickban_embed(match: dict) -> discord.Embed:
    fmt = match["format"].upper()
    team1 = match["teams"][0]
    team2 = match["teams"][1]
    remaining = match["remaining_maps"]
    picked = match["picked_maps"]
    step = match["step"]
    seq = PB_SEQUENCES[match["format"]]

    embed = discord.Embed(
        title=f"{team1['name']} vs {team2['name']} — {fmt} Map Selection",
        color=0x2B2D31,
    )

    embed.add_field(
        name="Available Maps",
        value=" | ".join(f"~~{m}~~" if m not in remaining else m for m in MAPS),
        inline=False,
    )

    if picked:
        picked_str = "\n".join(f"Map {i+1}: **{m}**" for i, m in enumerate(picked))
        embed.add_field(name="Maps Selected", value=picked_str, inline=False)

    if step < len(seq):
        team_idx, action = seq[step]
        current_team = match["teams"][team_idx]
        action_str = "BAN" if action == "ban" else "PICK"
        embed.add_field(
            name="Current Turn",
            value=f"**{current_team['name']}** — {action_str} a map",
            inline=False,
        )
    else:
        decider = remaining[0] if remaining else "Unknown"
        embed.add_field(name="Decider Map", value=f"**{decider}**", inline=False)

    return embed


class PickBanView(discord.ui.View):
    def __init__(self, match_id: str):
        super().__init__(timeout=None)
        self.match_id = match_id

    def rebuild(self):
        self.clear_items()
        match = active_matches.get(self.match_id)
        if not match:
            return

        step_info = get_current_step(match)
        if step_info is None:
            for i, map_name in enumerate(match["picked_maps"]):
                self.add_item(HostMapButton(
                    match_id=self.match_id,
                    map_index=i,
                    map_name=map_name,
                    label=f"Host Map {i+1}: {map_name}",
                    disabled=i != match.get("current_map_index", 0),
                ))
            decider = match["remaining_maps"][0] if match["remaining_maps"] else None
            if decider:
                total = len(match["picked_maps"])
                self.add_item(HostMapButton(
                    match_id=self.match_id,
                    map_index=total,
                    map_name=decider,
                    label=f"Host Map {total+1}: {decider} (Decider)",
                    disabled=total != match.get("current_map_index", 0),
                ))
            return

        team_idx, action = step_info
        current_captain_id = match["teams"][team_idx]["captain_id"]

        for map_name in match["remaining_maps"]:
            if action == "ban":
                self.add_item(MapActionButton(
                    match_id=self.match_id,
                    map_name=map_name,
                    action="ban",
                    captain_id=current_captain_id,
                    label=f"Ban {map_name}",
                    style=discord.ButtonStyle.danger,
                ))
            else:
                self.add_item(MapActionButton(
                    match_id=self.match_id,
                    map_name=map_name,
                    action="pick",
                    captain_id=current_captain_id,
                    label=f"Pick {map_name}",
                    style=discord.ButtonStyle.success,
                ))


class MapActionButton(discord.ui.Button):
    def __init__(self, match_id, map_name, action, captain_id, label, style):
        super().__init__(label=label, style=style, custom_id=f"pb_{match_id}_{map_name}_{action}")
        self.match_id = match_id
        self.map_name = map_name
        self.action = action
        self.captain_id = captain_id

    async def callback(self, interaction: discord.Interaction):
        match = active_matches.get(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        step_info = get_current_step(match)
        if not step_info:
            await interaction.response.send_message("Pick/ban is already complete.", ephemeral=True)
            return

        team_idx, action = step_info
        current_captain_id = match["teams"][team_idx]["captain_id"]

        if interaction.user.id != current_captain_id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return

        if self.action == "ban":
            match["remaining_maps"].remove(self.map_name)
            match["step"] += 1
        elif self.action == "pick":
            match["remaining_maps"].remove(self.map_name)
            match["picked_maps"].append(self.map_name)
            match["step"] += 1

        next_step = get_current_step(match)
        if next_step is None:
            match["current_map_index"] = 0

        view = PickBanView(match_id=self.match_id)
        view.rebuild()
        embed = build_pickban_embed(match)
        await interaction.response.edit_message(embed=embed, view=view)

        if next_step is None:
            decider = match["remaining_maps"][0] if match["remaining_maps"] else None
            all_maps = match["picked_maps"] + ([decider] if decider else [])
            maps_str = "\n".join(f"{i+1}. {m}" for i, m in enumerate(all_maps))
            fmt = match["format"].upper()
            summary = discord.Embed(
                title=f"{match['teams'][0]['name']} vs {match['teams'][1]['name']}",
                description=f"**Type: {fmt}**\n\n**Maps Selected:**\n{maps_str}",
                color=0x00FF7F,
            )
            await interaction.channel.send(embed=summary)


RAILWAY_BASE = "https://tourney-bot-production.up.railway.app"


class HostClientView(discord.ui.View):
    def __init__(self, glorp_url: str, crankshaft_url: str):
        super().__init__(timeout=60)
        self.add_item(discord.ui.Button(
            label="Open in Glorp",
            style=discord.ButtonStyle.link,
            url=glorp_url,
        ))
        self.add_item(discord.ui.Button(
            label="Open in CrankShaft",
            style=discord.ButtonStyle.link,
            url=crankshaft_url,
        ))


class HostMapButton(discord.ui.Button):
    def __init__(self, match_id, map_index, map_name, label, disabled):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"host_{match_id}_{map_index}",
            disabled=disabled,
        )
        self.match_id = match_id
        self.map_index = map_index
        self.map_name = map_name

    async def callback(self, interaction: discord.Interaction):
        match = active_matches.get(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        team1 = match["teams"][0]["name"]
        team2 = match["teams"][1]["name"]
        team_size = match["team_size"]

        params = {
            "action": "host-comp",
            "mapId": MAP_IDS.get(self.map_name, self.map_name),
            "team1Name": team1,
            "team2Name": team2,
            "teamSize": team_size,
            "webhook": WEBHOOK_URL,
        }
        query = urllib.parse.urlencode(params)
        glorp_url = f"{RAILWAY_BASE}/launch?client=glorp&{query}"
        crankshaft_url = f"{RAILWAY_BASE}/launch?client=crankshaft&{query}"

        view = HostClientView(glorp_url=glorp_url, crankshaft_url=crankshaft_url)
        await interaction.response.send_message(
            f"**Host Map: {self.map_name}** — Choose your client:",
            view=view,
            ephemeral=True,
        )

        match["current_map_index"] = self.map_index + 1
        view2 = PickBanView(match_id=self.match_id)
        view2.rebuild()
        await interaction.message.edit(view=view2)


# ── /match-create command ──────────────────────────────────────────────────────
@bot.tree.command(
    name="match-create",
    description="Start a pick/ban for a match between two teams.",
    guild=discord.Object(id=SERVER_ID),
)
@app_commands.describe(
    team1="Name of the higher seed team (bans first)",
    team2="Name of the lower seed team",
    format="Match format: bo1, bo3, or bo5",
)
async def match_create(interaction: discord.Interaction, team1: str, team2: str, format: str):
    fmt = format.lower()
    if fmt not in PB_SEQUENCES:
        await interaction.response.send_message("Format must be bo1, bo3, or bo5.", ephemeral=True)
        return

    found_t1 = None
    found_t2 = None
    found_tournament = None

    for t_id, t in tournaments.items():
        if t.get("open"):
            continue
        teams = t.get("teams", [])
        t1 = next((tm for tm in teams if tm["team_name"].strip().lower() == team1.strip().lower()), None)
        t2 = next((tm for tm in teams if tm["team_name"].strip().lower() == team2.strip().lower()), None)
        if t1 and t2:
            found_t1 = t1
            found_t2 = t2
            found_tournament = t
            break

    if not found_t1 or not found_t2:
        await interaction.response.send_message(
            "Could not find both teams. Make sure team names exactly match registration.",
            ephemeral=True,
        )
        return

    t1_captain_id = found_t1["players"][0]["discord_id"]
    t2_captain_id = found_t2["players"][0]["discord_id"]
    team_size = f"{found_tournament['team_size']}v{found_tournament['team_size']}"

    t1_player_ids = [p["discord_id"] for p in found_t1["players"]]
    t2_player_ids = [p["discord_id"] for p in found_t2["players"]]
    all_player_ids = t1_player_ids + t2_player_ids

    guild = interaction.guild
    staff_role = guild.get_role(STAFF_ROLE_ID)
    t_cat, _ = get_categories(guild)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    for pid in all_player_ids:
        member = guild.get_member(pid)
        if member:
            can_interact = pid in [t1_captain_id, t2_captain_id]
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=can_interact,
                add_reactions=False,
            )

    safe_t1 = re.sub(r"[^a-z0-9]", "", team1.lower())[:10]
    safe_t2 = re.sub(r"[^a-z0-9]", "", team2.lower())[:10]
    channel = await guild.create_text_channel(
        name=f"pb-{safe_t1}-vs-{safe_t2}",
        overwrites=overwrites,
        category=t_cat,
    )

    match_id = str(uuid.uuid4())[:8]
    match = {
        "match_id": match_id,
        "format": fmt,
        "team_size": team_size,
        "teams": [
            {"name": found_t1["team_name"], "captain_id": t1_captain_id},
            {"name": found_t2["team_name"], "captain_id": t2_captain_id},
        ],
        "remaining_maps": list(MAPS),
        "picked_maps": [],
        "step": 0,
        "current_map_index": 0,
        "channel_id": channel.id,
        "tournament_id": found_tournament["id"],
        "updates_channel_id": found_tournament.get("updates_channel_id"),
    }
    active_matches[match_id] = match

    view = PickBanView(match_id=match_id)
    view.rebuild()
    embed = build_pickban_embed(match)

    await channel.send(
        f"<@{t1_captain_id}> <@{t2_captain_id}> — Map selection has begun!\n"
        f"**{found_t1['team_name']}** (upper seed) bans first.",
        embed=embed,
        view=view,
    )

    await interaction.response.send_message(
        f"Match created in {channel.mention}",
        ephemeral=True,
    )


# ── Run ────────────────────────────────────────────────────────────────────────
async def main():
    import os
    TOKEN = os.environ.get("DISCORD_TOKEN")
    async with bot:
        await start_webhook_server()
        await bot.start(TOKEN)

asyncio.run(main())