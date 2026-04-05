import discord
from discord import app_commands
from discord.ext import commands
import uuid
import json
import re
import random
import asyncio
import os
import urllib.parse
from pathlib import Path
import aiohttp
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

# ── Challonge API ─────────────────────────────────────────────────────────────
CHALLONGE_API_KEY = os.environ.get("CHALLONGE_API_KEY", "")
CHALLONGE_BASE = "https://api.challonge.com/v1"


async def challonge_request(method: str, path: str, **kwargs) -> dict | list:
    url = f"{CHALLONGE_BASE}{path}"
    params = kwargs.pop("params", {})
    params["api_key"] = CHALLONGE_API_KEY
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, params=params, **kwargs) as resp:
            text = await resp.text()
            if resp.status >= 400:
                print(f"[Challonge] {method} {path} → {resp.status}: {text[:500]}")
                raise Exception(f"{resp.status}, {text[:200]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                raise Exception(f"Non-JSON response: {text[:200]}")


async def challonge_create_tournament(name: str, url_slug: str) -> dict:
    data = await challonge_request("POST", "/tournaments.json", data={
        "tournament[name]": name,
        "tournament[url]": url_slug,
        "tournament[tournament_type]": "double elimination",
        "tournament[private]": "false",
        "tournament[game_name]": "Krunker",
        "tournament[subdomain]": "krunker-biweeklies",
    })
    return data.get("tournament", data)


async def challonge_add_participants(challonge_id, teams_with_seeds: list[dict]) -> list:
    """teams_with_seeds: [{"name": "...", "seed": 1, "misc": "team_id"}, ...]"""
    added = []
    for t in teams_with_seeds:
        form = {
            "participant[name]": t["name"],
            "participant[seed]": str(t["seed"]),
        }
        if t.get("misc"):
            form["participant[misc]"] = t["misc"]
        data = await challonge_request("POST", f"/tournaments/{challonge_id}/participants.json", data=form)
        added.append(data)
    return added


async def challonge_start(challonge_id) -> dict:
    data = await challonge_request("POST", f"/tournaments/{challonge_id}/start.json", params={
        "include_matches": 1, "include_participants": 1,
    })
    return data.get("tournament", data)


async def challonge_get_matches(challonge_id, state="open") -> list:
    data = await challonge_request("GET", f"/tournaments/{challonge_id}/matches.json", params={
        "state": state,
    })
    return [m["match"] for m in data] if isinstance(data, list) else []


async def challonge_get_participants(challonge_id) -> list:
    data = await challonge_request("GET", f"/tournaments/{challonge_id}/participants.json")
    return [p["participant"] for p in data] if isinstance(data, list) else []


async def challonge_update_match(challonge_id, match_id, scores_csv: str, winner_id: int) -> dict:
    data = await challonge_request("PUT", f"/tournaments/{challonge_id}/matches/{match_id}.json", data={
        "match[scores_csv]": scores_csv,
        "match[winner_id]": str(winner_id),
    })
    return data.get("match", data)


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


# ── on_message ────────────────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # ── Krunker link handling in pick/ban match channels ──
    match_for_channel = next(
        (m for m in active_matches.values() if m.get("channel_id") == message.channel.id),
        None,
    )
    if match_for_channel and match_for_channel.get("host_captain_id"):
        krunker_link = re.search(r"https?://krunker\.io/\?game=\S+", message.content)
        if krunker_link:
            host_id = match_for_channel["host_captain_id"]
            if message.author.id != host_id:
                await message.delete()
                return

            # Host captain posted a link — check it's NY region
            link = krunker_link.group(0)
            game_code = re.search(r"\?game=(\w+):", link)
            if game_code and game_code.group(1) != "NY":
                await message.delete()
                await message.channel.send(
                    f"**Wrong host region! Host NY!**\n\n"
                    f"It's possible that your default region is set to Dallas or FRA. "
                    f"Switch your default region to New York, join any New York game server, "
                    f"and reclick the host button above. Contact <@723538323636748359> if you have any questions.",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                return

            # Valid NY link — ping all participants
            team1 = match_for_channel["teams"][0]
            team2 = match_for_channel["teams"][1]
            all_pings = " ".join(
                f"<@{pid}>" for pid in team1.get("player_ids", []) + team2.get("player_ids", [])
            )
            map_idx = match_for_channel.get("current_map_index", 0)
            all_maps = match_for_channel.get("all_maps", [])
            current_map = all_maps[map_idx] if map_idx < len(all_maps) else "Unknown"

            await message.channel.send(
                f"**{current_map}** is ready!\n"
                f"{link}\n\n"
                f"**{team1['name']}** (Team 1 / higher seed) join first.\n"
                f"**{team2['name']}** (Team 2) join second.\n\n"
                f"{all_pings}"
            )
            return


# ── /tournament-start ──────────────────────────────────────────────────────────
class SeedingView(discord.ui.View):
    """Interactive seeding panel — select a team, move it up/down, then confirm to start."""
    def __init__(self, tournament_id: str, tournament: dict, interaction_user_id: int):
        super().__init__(timeout=600)
        self.tournament_id = tournament_id
        self.tournament = tournament
        self.interaction_user_id = interaction_user_id
        self.seeded_teams = list(tournament.get("teams", []))
        self.selected_index = None
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        options = []
        for i, tm in enumerate(self.seeded_teams[:25]):
            label = f"{i+1}. {tm['team_name']}"[:100]
            options.append(discord.SelectOption(label=label, value=str(i)))
        self.add_item(SeedTeamSelect(options))
        self.add_item(SeedMoveButton("up", disabled=self.selected_index is None or self.selected_index == 0))
        self.add_item(SeedMoveButton("down", disabled=self.selected_index is None or self.selected_index == len(self.seeded_teams) - 1))
        self.add_item(SeedConfirmButton())

    def build_embed(self):
        lines = []
        for i, tm in enumerate(self.seeded_teams):
            prefix = "▸ " if i == self.selected_index else "  "
            lines.append(f"`{prefix}{i+1:>2}.` **{tm['team_name']}**")
        embed = discord.Embed(
            title="Seeding — Reorder Teams",
            description="\n".join(lines),
            color=0x2B2D31,
        )
        embed.set_footer(text="Select a team, use ▲/▼ to move, then Confirm to create the bracket.")
        return embed


class SeedTeamSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Select a team to move...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: SeedingView = self.view
        view.selected_index = int(self.values[0])
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class SeedMoveButton(discord.ui.Button):
    def __init__(self, direction: str, disabled: bool):
        label = "▲ Move Up" if direction == "up" else "▼ Move Down"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, disabled=disabled, row=1,
                         custom_id=f"seed_move_{direction}")
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: SeedingView = self.view
        idx = view.selected_index
        if idx is None:
            await interaction.response.defer()
            return
        if self.direction == "up" and idx > 0:
            view.seeded_teams[idx], view.seeded_teams[idx - 1] = view.seeded_teams[idx - 1], view.seeded_teams[idx]
            view.selected_index = idx - 1
        elif self.direction == "down" and idx < len(view.seeded_teams) - 1:
            view.seeded_teams[idx], view.seeded_teams[idx + 1] = view.seeded_teams[idx + 1], view.seeded_teams[idx]
            view.selected_index = idx + 1
        view._rebuild()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class SeedConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Confirm & Start", style=discord.ButtonStyle.success, row=1, custom_id="seed_confirm")

    async def callback(self, interaction: discord.Interaction):
        view: SeedingView = self.view
        if interaction.user.id != view.interaction_user_id:
            await interaction.response.send_message("Only the organizer can confirm.", ephemeral=True)
            return

        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

        t = view.tournament
        t_id = view.tournament_id
        t["open"] = False
        t["teams"] = view.seeded_teams
        save_tournaments()

        team_count = len(t["teams"])
        guild = interaction.guild
        staff_role = guild.get_role(STAFF_ROLE_ID)
        t_cat, a_cat = get_categories(guild)

        read_only = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
            guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
        }
        admin_perms = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if staff_role:
            admin_perms[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        updates_channel = await guild.create_text_channel(name="tournament-updates", overwrites=read_only, category=t_cat)
        admin_channel = await guild.create_text_channel(name="tournament-admin", overwrites=admin_perms, category=a_cat)

        t["updates_channel_id"] = updates_channel.id
        t["admin_channel_id"] = admin_channel.id
        save_tournaments()

        # Generate admin token for bracket page
        t["admin_token"] = str(uuid.uuid4())
        save_tournaments()

        # Create Challonge bracket
        slug = re.sub(r"[^a-z0-9]", "_", t["name"].lower()).strip("_")[:60]
        try:
            print(f"[Challonge] Creating tournament: {t['name']} ({slug})")
            ch_tourney = await challonge_create_tournament(t["name"], slug)
            challonge_id = ch_tourney["id"]
            t["challonge_id"] = challonge_id
            t["challonge_url"] = ch_tourney.get("full_challonge_url", f"https://krunker-biweeklies.challonge.com/{slug}")
            print(f"[Challonge] Tournament created: {challonge_id}")

            participants = [
                {"name": tm["team_name"], "seed": i + 1, "misc": tm["team_id"]}
                for i, tm in enumerate(view.seeded_teams)
            ]
            print(f"[Challonge] Adding {len(participants)} participants...")
            added = await challonge_add_participants(challonge_id, participants)
            print(f"[Challonge] Participants added: {len(added)}")

            # Build mapping: team_id -> challonge participant_id
            participant_map = {}
            if isinstance(added, list):
                for entry in added:
                    p = entry.get("participant", entry)
                    if p.get("misc"):
                        participant_map[p["misc"]] = p["id"]
            t["challonge_participant_map"] = participant_map
            print(f"[Challonge] Participant map: {participant_map}")

            print(f"[Challonge] Starting tournament...")
            await challonge_start(challonge_id)
            save_tournaments()
            print(f"[Challonge] Tournament started successfully")

            bracket_msg = f"**{t['name']}** has started with {team_count} teams!\n**Bracket:** {t['challonge_url']}"
        except Exception as e:
            print(f"[Challonge] Error: {e}")
            import traceback
            traceback.print_exc()
            bracket_msg = f"**{t['name']}** has started with {team_count} teams.\n⚠️ Challonge bracket creation failed: {e}"

        signups_channel = guild.get_channel(t["signups_channel_id"])
        if signups_channel:
            await signups_channel.send(f"Sign-ups closed — {team_count} team{'s' if team_count != 1 else ''} registered.")

        await updates_channel.send(bracket_msg)
        await interaction.followup.send(
            f"Tournament started.\nUpdates: {updates_channel.mention}\nAdmin: {admin_channel.mention}"
            f"\nBracket: {t.get('challonge_url', 'N/A')}"
            f"\n**Admin Panel:** {RAILWAY_BASE}/admin/{t_id}?token={t['admin_token']}",
            ephemeral=True,
        )
        view.stop()


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

    view = SeedingView(tournament_id=t_id, tournament=t, interaction_user_id=interaction.user.id)
    await interaction.response.send_message(embed=view.build_embed(), view=view)


MAPS = ["Bureau", "Lush", "Site", "Industry", "Undergrowth", "Sandstorm", "Burg"]

# ── /test-scoreboard ──────────────────────────────────────────────────────────
FAKE_PLAYERS = [
    {"name": "AraffyWappy",   "score": 6655, "kills": 45, "deaths": 30, "objective_score": 1340, "damage_done": 4183},
    {"name": "LESHAWN",       "score": 6085, "kills": 37, "deaths": 39, "objective_score": 1340, "damage_done": 4582},
    {"name": "TravisScottAl", "score": 5920, "kills": 40, "deaths": 41, "objective_score": 1070, "damage_done": 3781},
    {"name": "rckyyyyyy",     "score": 5805, "kills": 37, "deaths": 30, "objective_score": 1540, "damage_done": 3659},
    {"name": "VollerPlays",   "score": 5950, "kills": 41, "deaths": 43, "objective_score": 1210, "damage_done": 4694},
    {"name": "HypeZeus",      "score": 5870, "kills": 41, "deaths": 50, "objective_score": 880,  "damage_done": 4926},
    {"name": "MemoMINI",      "score": 5745, "kills": 41, "deaths": 47, "objective_score": 910,  "damage_done": 4836},
    {"name": "ECODOT",        "score": 4890, "kills": 36, "deaths": 53, "objective_score": 920,  "damage_done": 4257},
]

@bot.tree.command(name="test-scoreboard", description="Post a test scoreboard to see how it looks.", guild=discord.Object(id=SERVER_ID))
@app_commands.describe(
    team_size="Team size for the test scoreboard",
    map_name="Map to use for the background",
)
@app_commands.choices(
    team_size=[
        app_commands.Choice(name="2v2", value=2),
        app_commands.Choice(name="3v3", value=3),
        app_commands.Choice(name="4v4", value=4),
    ],
    map_name=[app_commands.Choice(name=m, value=m) for m in MAPS],
)
async def test_scoreboard(interaction: discord.Interaction, team_size: int, map_name: str):
    await interaction.response.defer(ephemeral=True)

    team1_players = FAKE_PLAYERS[:team_size]
    team2_players = FAKE_PLAYERS[4:4 + team_size]

    try:
        from scoreboard import draw_scoreboard, RED, WHITE
        img_buf = draw_scoreboard(
            tournament_name=interaction.guild.name,
            map_name=map_name,
            team1_name="Team Alpha",
            team1_score=2,
            team1_players=team1_players,
            team1_color=RED,
            team2_name="Team Bravo",
            team2_score=1,
            team2_players=team2_players,
            team2_color=WHITE,
        )
        await interaction.channel.send(file=discord.File(img_buf, filename="scoreboard.png"))
        await interaction.followup.send("Test scoreboard posted.", ephemeral=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"Failed to generate scoreboard: {e}", ephemeral=True)


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


# ── Error handler ─────────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    import traceback
    traceback.print_exc()
    print(f"[Error] {error}")


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

    import json as _json
    raw_json = _json.dumps(payload, indent=2)
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name="test-raw-data")
        if ch:
            # Discord message limit is 2000 chars, split if needed
            for i in range(0, len(raw_json), 1990):
                await ch.send(f"```json\n{raw_json[i:i+1990]}\n```")

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

    winner_players = sorted(
        [p for p in players if p["team"] == winner_team_num],
        key=lambda p: p.get("score", 0), reverse=True
    )
    loser_players = sorted(
        [p for p in players if p["team"] != winner_team_num],
        key=lambda p: p.get("score", 0), reverse=True
    )

    # Generate scoreboard image
    try:
        from scoreboard import draw_scoreboard, RED, WHITE
        img_buf = draw_scoreboard(
            tournament_name=matched_tournament["name"],
            map_name=map_name,
            team1_name=winner_discord["team_name"],
            team1_score=winner_score,
            team1_players=winner_players,
            team1_color=RED,
            team2_name=loser_discord["team_name"],
            team2_score=loser_score,
            team2_players=loser_players,
            team2_color=WHITE,
        )
        image_file = discord.File(img_buf, filename="scoreboard.png")
        print(f"[Scoreboard] Image generated successfully")
    except Exception as e:
        print(f"[Scoreboard] Failed to generate image: {e}")
        import traceback
        traceback.print_exc()
        image_file = None

    for g in bot.guilds:
        ch = g.get_channel(matched_tournament["updates_channel_id"])
        if ch:
            if image_file:
                await ch.send(file=image_file)
            else:
                await ch.send(f"**{winner_discord['team_name']}** won {score_str} against **{loser_discord['team_name']}**")
            print(f"[Webhook] Posted result: {winner_discord['team_name']} won {score_str} vs {loser_discord['team_name']}")
            break

    # Track series score and report to Challonge when series is decided
    active_match = next(
        (m for m in active_matches.values()
         if m.get("challonge_match_id")
         and {m["teams"][0]["name"].lower(), m["teams"][1]["name"].lower()}
         == {winner_discord["team_name"].lower(), loser_discord["team_name"].lower()}),
        None,
    )

    if active_match:
        series = active_match.get("series_score", {})
        winner_key = winner_discord["team_name"].lower()
        loser_key = loser_discord["team_name"].lower()
        series[winner_key] = series.get(winner_key, 0) + 1
        series[loser_key] = series.get(loser_key, 0)
        active_match["series_score"] = series

        fmt = active_match.get("format", "bo1")
        wins_needed = {"bo1": 1, "bo3": 2, "bo5": 3}.get(fmt, 1)
        winner_series_wins = series[winner_key]
        loser_series_wins = series[loser_key]

        print(f"[Series] {winner_discord['team_name']} {winner_series_wins} - {loser_series_wins} {loser_discord['team_name']} ({fmt}, need {wins_needed})")

        # Post series update to match channel
        for g in bot.guilds:
            match_ch = g.get_channel(active_match.get("channel_id"))
            if match_ch:
                if winner_series_wins >= wins_needed:
                    await match_ch.send(
                        f"**{winner_discord['team_name']}** wins the series **{winner_series_wins}-{loser_series_wins}**! 🎉"
                    )
                else:
                    await match_ch.send(
                        f"**{winner_discord['team_name']}** wins this map! "
                        f"Series: **{winner_series_wins}-{loser_series_wins}**"
                    )
                break

        # Only report to Challonge when the series is decided
        if winner_series_wins >= wins_needed:
            challonge_id = active_match.get("challonge_id")
            ch_match_id = active_match.get("challonge_match_id")
            participant_map = active_match.get("challonge_participant_map", {})
            winner_ch_pid = participant_map.get(winner_discord.get("team_id"))

            if challonge_id and ch_match_id and winner_ch_pid:
                try:
                    open_matches = await challonge_get_matches(challonge_id, state="all")
                    ch_match = next((m for m in open_matches if m["id"] == ch_match_id), None)
                    if ch_match:
                        # scores_csv: player1's score first
                        if ch_match["player1_id"] == winner_ch_pid:
                            csv = f"{winner_series_wins}-{loser_series_wins}"
                        else:
                            csv = f"{loser_series_wins}-{winner_series_wins}"
                        await challonge_update_match(challonge_id, ch_match_id, csv, winner_ch_pid)
                        print(f"[Challonge] Series decided — reported {csv} for match {ch_match_id}")
                except Exception as e:
                    print(f"[Challonge] Failed to report result: {e}")

            # Clean up the active match
            match_id = active_match.get("match_id")
            if match_id and match_id in active_matches:
                del active_matches[match_id]

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


# ── Admin bracket page ────────────────────────────────────────────────────────
async def handle_admin_bracket(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.Response(status=403, text="Invalid tournament or token.")

    html = BRACKET_HTML.replace("{{TOURNAMENT_ID}}", t_id).replace("{{TOKEN}}", token)
    return web.Response(content_type="text/html", text=html)


async def handle_api_tournament(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    challonge_id = t.get("challonge_id")
    if not challonge_id:
        return web.json_response({"error": "no bracket"}, status=404)

    try:
        matches = await challonge_get_matches(challonge_id, state="all")
        participants = await challonge_get_participants(challonge_id)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    p_map = {p["id"]: p["name"] for p in participants}

    # Check which matches are already started in Discord
    started_ch_ids = {m.get("challonge_match_id") for m in active_matches.values() if m.get("challonge_match_id")}

    match_list = []
    for m in matches:
        match_list.append({
            "id": m["id"],
            "round": m.get("round", 0),
            "identifier": m.get("identifier", ""),
            "state": m["state"],
            "player1": p_map.get(m.get("player1_id"), None),
            "player2": p_map.get(m.get("player2_id"), None),
            "player1_id": m.get("player1_id"),
            "player2_id": m.get("player2_id"),
            "scores_csv": m.get("scores_csv", ""),
            "winner_id": m.get("winner_id"),
            "started_in_discord": m["id"] in started_ch_ids,
        })

    return web.json_response({
        "name": t["name"],
        "challonge_url": t.get("challonge_url", ""),
        "matches": match_list,
    })


async def handle_api_start_match(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    t_id = body.get("tournament_id", "").upper()
    token = body.get("token", "")
    ch_match_id = body.get("challonge_match_id")
    fmt = body.get("format", "bo1")

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    if fmt not in ("bo1", "bo3", "bo5"):
        return web.json_response({"error": "invalid format"}, status=400)

    # Get match info from Challonge
    challonge_id = t.get("challonge_id")
    if not challonge_id:
        return web.json_response({"error": "no bracket"}, status=404)

    try:
        matches = await challonge_get_matches(challonge_id, state="open")
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    ch_match = next((m for m in matches if m["id"] == ch_match_id), None)
    if not ch_match:
        return web.json_response({"error": "match not found or not open"}, status=404)

    # Resolve participants to local team dicts
    participant_map = t.get("challonge_participant_map", {})
    ch_to_local = {}
    for team_id, ch_pid in participant_map.items():
        team_dict = next((tm for tm in t["teams"] if tm["team_id"] == team_id), None)
        if team_dict:
            ch_to_local[ch_pid] = team_dict

    t1_dict = ch_to_local.get(ch_match.get("player1_id"))
    t2_dict = ch_to_local.get(ch_match.get("player2_id"))
    if not t1_dict or not t2_dict:
        return web.json_response({"error": "could not resolve teams"}, status=404)

    guild = bot.get_guild(SERVER_ID)
    if not guild:
        return web.json_response({"error": "bot not in target guild"}, status=500)

    try:
        ch = await create_match(guild, t1_dict, t2_dict, t, fmt, challonge_match_id=ch_match_id)
        return web.json_response({"ok": True, "channel": ch.name})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


BRACKET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tournament Admin</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a2e; color: #eee; font-family: 'Segoe UI', system-ui, sans-serif; padding: 24px; min-height: 100vh; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  .subtitle { color: #888; margin-bottom: 12px; font-size: 0.9rem; }
  .subtitle a { color: #f09030; text-decoration: none; }
  .subtitle a:hover { text-decoration: underline; }

  .section-label {
    font-size: 1rem; font-weight: 700; color: #f09030; text-transform: uppercase;
    letter-spacing: 1.5px; margin: 28px 0 4px; padding: 6px 12px;
    border-left: 3px solid #f09030;
  }

  /* ── Bracket layout ─────────────────────────────────────── */
  .bracket-wrapper { overflow-x: auto; padding: 8px 0 16px; }
  .bracket {
    display: flex; align-items: stretch; position: relative;
    width: max-content;
  }
  .round {
    display: flex; flex-direction: column; justify-content: center;
    min-width: 220px; position: relative; z-index: 1;
  }
  .round-hdr {
    text-align: center; font-size: 0.7rem; color: #f09030; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1px; padding-bottom: 8px;
  }
  .round-matches { display: flex; flex-direction: column; justify-content: space-around; flex: 1; }

  /* Connector column between rounds */
  .connector-col { width: 32px; position: relative; flex-shrink: 0; }
  .connector-col svg { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
  .connector-col svg line { stroke: #3a3a5a; stroke-width: 1.5; }

  /* ── Match card ─────────────────────────────────────────── */
  .match-card {
    background: #0f1a30; border: 1px solid #2a2a4a; border-radius: 4px;
    width: 200px; margin: 4px auto; cursor: default; transition: all 0.15s;
    position: relative; overflow: hidden;
  }
  .match-card.open { border-color: #f09030; cursor: pointer; }
  .match-card.open:hover { background: #162040; box-shadow: 0 0 10px rgba(240,144,48,0.25); transform: scale(1.02); }
  .match-card.started { border-color: #4CAF50; }
  .match-card.complete { opacity: 0.55; }
  .match-id {
    position: absolute; top: 2px; right: 5px; font-size: 0.6rem; color: #444;
    font-weight: 600;
  }
  .team-row {
    display: flex; align-items: center; padding: 5px 8px; font-size: 0.8rem;
    border-bottom: 1px solid #1a2040;
  }
  .team-row:last-of-type { border-bottom: none; }
  .team-row.top { border-bottom: 1px solid #2a2a4a; }
  .team-seed { color: #555; font-size: 0.65rem; width: 18px; text-align: right; margin-right: 6px; flex-shrink: 0; }
  .team-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .team-name.winner { color: #f09030; font-weight: 700; }
  .team-name.loser { color: #555; }
  .team-name.tbd { color: #444; font-style: italic; }
  .team-score {
    font-weight: 700; min-width: 24px; text-align: center; padding: 1px 5px;
    border-radius: 2px; font-size: 0.75rem; margin-left: 4px;
  }
  .team-score.win { background: #f09030; color: #000; }
  .match-status {
    font-size: 0.6rem; text-align: center; padding: 2px; text-transform: uppercase;
    letter-spacing: 0.5px; font-weight: 600;
  }
  .match-status.live { background: #4CAF50; color: #fff; }
  .match-status.open-status { background: rgba(240,144,48,0.15); color: #f09030; }

  /* ── Format picker modal ────────────────────────────────── */
  .modal-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.75); z-index: 100; align-items: center; justify-content: center;
  }
  .modal-overlay.active { display: flex; }
  .modal {
    background: #0f1a30; border: 1px solid #f09030; border-radius: 10px;
    padding: 24px; min-width: 320px; text-align: center;
  }
  .modal h2 { font-size: 1.1rem; margin-bottom: 4px; }
  .modal .match-label { color: #888; font-size: 0.85rem; margin-bottom: 16px; }
  .modal .fmt-buttons { display: flex; gap: 10px; justify-content: center; margin-bottom: 16px; }
  .modal .fmt-btn {
    padding: 10px 24px; border-radius: 6px; border: 2px solid #2a2a4a;
    background: #1a1a2e; color: #eee; font-size: 1rem; cursor: pointer; transition: all 0.15s;
  }
  .modal .fmt-btn:hover { border-color: #f09030; background: #1a2a50; }
  .modal .fmt-btn.selected { border-color: #f09030; background: #f09030; color: #000; font-weight: 700; }
  .modal .start-btn {
    padding: 10px 32px; border-radius: 6px; border: none;
    background: #4CAF50; color: #fff; font-size: 1rem; font-weight: 700;
    cursor: pointer; transition: all 0.15s;
  }
  .modal .start-btn:hover { background: #45a049; }
  .modal .start-btn:disabled { background: #333; cursor: not-allowed; }
  .modal .cancel-btn {
    padding: 8px 20px; border-radius: 6px; border: 1px solid #555;
    background: transparent; color: #888; font-size: 0.85rem;
    cursor: pointer; margin-top: 10px;
  }
</style>
</head>
<body>
<h1 id="title">Loading...</h1>
<p class="subtitle" id="subtitle"></p>

<div class="section-label">Winners Bracket</div>
<div class="bracket-wrapper"><div class="bracket" id="winners-bracket"></div></div>

<div class="section-label">Losers Bracket</div>
<div class="bracket-wrapper"><div class="bracket" id="losers-bracket"></div></div>

<div class="section-label" id="gf-label">Grand Finals</div>
<div class="bracket-wrapper"><div class="bracket" id="grand-finals"></div></div>

<div class="modal-overlay" id="modal">
  <div class="modal">
    <h2>Start Match</h2>
    <p class="match-label" id="modal-match-label"></p>
    <div class="fmt-buttons">
      <button class="fmt-btn" data-fmt="bo1">BO1</button>
      <button class="fmt-btn" data-fmt="bo3">BO3</button>
      <button class="fmt-btn" data-fmt="bo5">BO5</button>
    </div>
    <button class="start-btn" id="modal-start" disabled>Start Match</button><br>
    <button class="cancel-btn" id="modal-cancel">Cancel</button>
  </div>
</div>

<script>
const T_ID = "{{TOURNAMENT_ID}}";
const TOKEN = "{{TOKEN}}";
let selectedFmt = null;
let selectedMatchId = null;
const MATCH_CARD_H = 58;  // approx height of a match card + margin
const CONNECTOR_W = 32;

async function loadBracket() {
  const resp = await fetch(`/api/tournament/${T_ID}?token=${TOKEN}`);
  if (!resp.ok) { document.getElementById("title").textContent = "Error loading bracket"; return; }
  const data = await resp.json();

  document.getElementById("title").textContent = data.name;
  document.getElementById("subtitle").innerHTML = `<a href="${data.challonge_url}" target="_blank">View on Challonge &#8599;</a> &nbsp;|&nbsp; Auto-refreshes every 30s`;

  const winners = {}, losers = {}, gf = [];
  for (const m of data.matches) {
    if (m.round > 0) {
      if (!winners[m.round]) winners[m.round] = [];
      winners[m.round].push(m);
    } else if (m.round < 0) {
      if (!losers[m.round]) losers[m.round] = [];
      losers[m.round].push(m);
    } else {
      gf.push(m);
    }
  }

  // Detect grand finals: highest positive round with <=2 matches
  const wRounds = Object.keys(winners).map(Number).sort((a,b) => a-b);
  if (wRounds.length) {
    const maxW = wRounds[wRounds.length - 1];
    if (winners[maxW] && winners[maxW].length <= 2) {
      gf.push(...winners[maxW]);
      delete winners[maxW];
    }
  }

  renderBracketWithLines(document.getElementById("winners-bracket"), winners, false);
  renderBracketWithLines(document.getElementById("losers-bracket"), losers, true);
  renderGrandFinals(document.getElementById("grand-finals"), gf);

  const gfLabel = document.getElementById("gf-label");
  gfLabel.style.display = gf.length ? "" : "none";
}

function makeMatchCard(m) {
  const card = document.createElement("div");
  card.className = "match-card";
  if (m.state === "open" && !m.started_in_discord) card.className += " open";
  else if (m.started_in_discord) card.className += " started";
  else if (m.state === "complete") card.className += " complete";

  const scores = m.scores_csv ? m.scores_csv.split("-") : ["", ""];
  const p1Won = m.winner_id && m.winner_id === m.player1_id;
  const p2Won = m.winner_id && m.winner_id === m.player2_id;

  let statusHtml = "";
  if (m.started_in_discord) statusHtml = '<div class="match-status live">LIVE</div>';
  else if (m.state === "open") statusHtml = '<div class="match-status open-status">READY</div>';

  card.innerHTML =
    '<span class="match-id">' + m.identifier + '</span>' +
    '<div class="team-row top">' +
      '<span class="team-name ' + (m.player1 ? (p1Won ? 'winner' : (p2Won ? 'loser' : '')) : 'tbd') + '">' + (m.player1 || 'TBD') + '</span>' +
      '<span class="team-score ' + (p1Won ? 'win' : '') + '">' + (scores[0] || '') + '</span>' +
    '</div>' +
    '<div class="team-row">' +
      '<span class="team-name ' + (m.player2 ? (p2Won ? 'winner' : (p1Won ? 'loser' : '')) : 'tbd') + '">' + (m.player2 || 'TBD') + '</span>' +
      '<span class="team-score ' + (p2Won ? 'win' : '') + '">' + (scores[1] || '') + '</span>' +
    '</div>' +
    statusHtml;

  if (m.state === "open" && !m.started_in_discord) {
    card.addEventListener("click", () => openModal(m));
  }
  return card;
}

function renderBracketWithLines(container, roundsObj, isLosers) {
  container.innerHTML = "";
  const roundNums = Object.keys(roundsObj).map(Number).sort((a,b) => isLosers ? b-a : a-b);
  if (!roundNums.length) return;

  for (let ri = 0; ri < roundNums.length; ri++) {
    const r = roundNums[ri];
    const matches = roundsObj[r];

    // Round column
    const roundDiv = document.createElement("div");
    roundDiv.className = "round";

    const hdr = document.createElement("div");
    hdr.className = "round-hdr";
    hdr.textContent = isLosers ? "Losers R" + Math.abs(r) : "Winners R" + r;
    roundDiv.appendChild(hdr);

    const matchesDiv = document.createElement("div");
    matchesDiv.className = "round-matches";
    for (const m of matches) {
      matchesDiv.appendChild(makeMatchCard(m));
    }
    roundDiv.appendChild(matchesDiv);
    container.appendChild(roundDiv);

    // Draw connector lines between this round and the next
    if (ri < roundNums.length - 1) {
      const nextR = roundNums[ri + 1];
      const nextCount = roundsObj[nextR].length;
      const thisCount = matches.length;

      const connCol = document.createElement("div");
      connCol.className = "connector-col";
      container.appendChild(connCol);

      // We draw SVG lines after layout so we know positions
      requestAnimationFrame(() => drawConnectors(connCol, matchesDiv, container, roundNums, ri, roundsObj, isLosers));
    }
  }
}

function drawConnectors(connCol, prevMatchesDiv, container, roundNums, ri, roundsObj, isLosers) {
  const prevCards = prevMatchesDiv.querySelectorAll(".match-card");
  // Find the next round's matches div
  const allRoundDivs = container.querySelectorAll(".round");
  const nextRoundDiv = allRoundDivs[ri + 1];
  if (!nextRoundDiv) return;
  const nextMatchesDiv = nextRoundDiv.querySelector(".round-matches");
  const nextCards = nextMatchesDiv.querySelectorAll(".match-card");

  const connRect = connCol.getBoundingClientRect();
  if (connRect.height === 0) return;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", connRect.width);
  svg.setAttribute("height", connRect.height);
  svg.style.position = "absolute";
  svg.style.top = "0";
  svg.style.left = "0";
  svg.style.width = "100%";
  svg.style.height = "100%";
  connCol.appendChild(svg);

  const w = connRect.width;

  // If this is a standard bracket (2 matches feed into 1), draw traditional connectors
  // Otherwise just draw horizontal lines
  if (prevCards.length === nextCards.length * 2) {
    // Classic bracket merge: pairs of matches -> one match
    for (let i = 0; i < nextCards.length; i++) {
      const top = prevCards[i * 2];
      const bot = prevCards[i * 2 + 1];
      const dest = nextCards[i];
      if (!top || !bot || !dest) continue;

      const topY = top.getBoundingClientRect().top + top.getBoundingClientRect().height / 2 - connRect.top;
      const botY = bot.getBoundingClientRect().top + bot.getBoundingClientRect().height / 2 - connRect.top;
      const destY = dest.getBoundingClientRect().top + dest.getBoundingClientRect().height / 2 - connRect.top;
      const midX = w / 2;

      // Horizontal from top match
      addLine(svg, 0, topY, midX, topY);
      // Horizontal from bottom match
      addLine(svg, 0, botY, midX, botY);
      // Vertical connecting them
      addLine(svg, midX, topY, midX, botY);
      // Horizontal to next match
      addLine(svg, midX, destY, w, destY);
    }
  } else {
    // Non-standard (losers bracket often has 1:1 rounds) — just draw straight lines
    for (let i = 0; i < Math.min(prevCards.length, nextCards.length); i++) {
      const src = prevCards[i];
      const dest = nextCards[i];
      if (!src || !dest) continue;
      const srcY = src.getBoundingClientRect().top + src.getBoundingClientRect().height / 2 - connRect.top;
      const destY = dest.getBoundingClientRect().top + dest.getBoundingClientRect().height / 2 - connRect.top;
      addLine(svg, 0, srcY, w / 2, srcY);
      if (Math.abs(srcY - destY) > 2) {
        addLine(svg, w / 2, srcY, w / 2, destY);
      }
      addLine(svg, w / 2, destY, w, destY);
    }
  }
}

function addLine(svg, x1, y1, x2, y2) {
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  line.setAttribute("x1", x1);
  line.setAttribute("y1", y1);
  line.setAttribute("x2", x2);
  line.setAttribute("y2", y2);
  svg.appendChild(line);
}

function renderGrandFinals(container, matches) {
  container.innerHTML = "";
  if (!matches.length) return;
  const roundDiv = document.createElement("div");
  roundDiv.className = "round";
  const hdr = document.createElement("div");
  hdr.className = "round-hdr";
  hdr.textContent = "Grand Finals";
  roundDiv.appendChild(hdr);
  const matchesDiv = document.createElement("div");
  matchesDiv.className = "round-matches";
  for (const m of matches) matchesDiv.appendChild(makeMatchCard(m));
  roundDiv.appendChild(matchesDiv);
  container.appendChild(roundDiv);
}

function openModal(m) {
  selectedMatchId = m.id;
  selectedFmt = null;
  document.getElementById("modal-match-label").textContent = (m.player1 || "TBD") + " vs " + (m.player2 || "TBD");
  document.getElementById("modal-start").disabled = true;
  document.querySelectorAll(".fmt-btn").forEach(b => b.classList.remove("selected"));
  document.getElementById("modal").classList.add("active");
}

document.querySelectorAll(".fmt-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    selectedFmt = btn.dataset.fmt;
    document.querySelectorAll(".fmt-btn").forEach(b => b.classList.remove("selected"));
    btn.classList.add("selected");
    document.getElementById("modal-start").disabled = false;
  });
});

document.getElementById("modal-cancel").addEventListener("click", () => {
  document.getElementById("modal").classList.remove("active");
});

document.getElementById("modal-start").addEventListener("click", async () => {
  if (!selectedMatchId || !selectedFmt) return;
  const btn = document.getElementById("modal-start");
  btn.disabled = true;
  btn.textContent = "Starting...";
  try {
    const resp = await fetch("/api/start-match", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        tournament_id: T_ID,
        token: TOKEN,
        challonge_match_id: selectedMatchId,
        format: selectedFmt,
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById("modal").classList.remove("active");
      btn.textContent = "Start Match";
      loadBracket();
    } else {
      alert("Error: " + (data.error || "Unknown error"));
      btn.textContent = "Start Match";
      btn.disabled = false;
    }
  } catch(e) {
    alert("Error: " + e.message);
    btn.textContent = "Start Match";
    btn.disabled = false;
  }
});

loadBracket();
setInterval(loadBracket, 30000);
</script>
</body>
</html>"""


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/krunker", handle_krunker_webhook)
    app.router.add_get("/launch", handle_launch)
    app.router.add_get("/admin/{tournament_id}", handle_admin_bracket)
    app.router.add_get("/api/tournament/{tournament_id}", handle_api_tournament)
    app.router.add_post("/api/start-match", handle_api_start_match)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"Webhook server running on port {WEBHOOK_PORT}")


# ── Pick/Ban system ────────────────────────────────────────────────────────────

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
        label = "Map" if match["format"] == "bo1" else "Decider Map"
        embed.add_field(name=label, value=f"**{decider}**", inline=False)

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
                is_bo1 = match["format"] == "bo1"
                decider_label = f"Host Map: {decider}" if is_bo1 else f"Host Map {total+1}: {decider} (Decider)"
                self.add_item(HostMapButton(
                    match_id=self.match_id,
                    map_index=total,
                    map_name=decider,
                    label=decider_label,
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

            # Randomly assign a captain to host
            # Higher seed (team index 0) always hosts
            host_captain_id = match["teams"][0]["captain_id"]
            match["host_captain_id"] = host_captain_id
            match["current_map_index"] = 0
            match["all_maps"] = all_maps

            first_map = all_maps[0]
            await interaction.channel.send(
                f"<@{host_captain_id}> has been randomly selected to **host**.\n"
                f"Please host **{first_map}** on **NY** servers and post the game link here."
            )


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

        host_id = match.get("host_captain_id")
        if host_id and interaction.user.id != host_id:
            await interaction.response.send_message("Only the assigned host can use this button.", ephemeral=True)
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
            "region": "ny",
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


# ── Match creation helper ─────────────────────────────────────────────────────
async def create_match(guild: discord.Guild, found_t1: dict, found_t2: dict, found_tournament: dict, fmt: str, challonge_match_id: int = None) -> discord.TextChannel:
    """Create a match channel and start pick/ban. Returns the created channel."""
    t1_captain_id = found_t1["players"][0]["discord_id"]
    t2_captain_id = found_t2["players"][0]["discord_id"]
    team_size = f"{found_tournament['team_size']}v{found_tournament['team_size']}"

    t1_player_ids = [p["discord_id"] for p in found_t1["players"]]
    t2_player_ids = [p["discord_id"] for p in found_t2["players"]]
    all_player_ids = t1_player_ids + t2_player_ids

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

    channel = await guild.create_text_channel(
        name=f"{found_t1['team_name']} vs {found_t2['team_name']}",
        overwrites=overwrites,
        category=t_cat,
    )

    match_id = str(uuid.uuid4())[:8]
    match = {
        "match_id": match_id,
        "format": fmt,
        "team_size": team_size,
        "teams": [
            {"name": found_t1["team_name"], "captain_id": t1_captain_id, "player_ids": t1_player_ids},
            {"name": found_t2["team_name"], "captain_id": t2_captain_id, "player_ids": t2_player_ids},
        ],
        "remaining_maps": list(MAPS),
        "picked_maps": [],
        "step": 0,
        "current_map_index": 0,
        "channel_id": channel.id,
        "tournament_id": found_tournament["id"],
        "updates_channel_id": found_tournament.get("updates_channel_id"),
        "challonge_match_id": challonge_match_id,
        "challonge_id": found_tournament.get("challonge_id"),
        "challonge_participant_map": found_tournament.get("challonge_participant_map", {}),
        "series_score": {},  # {team_name_lower: maps_won}
    }
    active_matches[match_id] = match

    view = PickBanView(match_id=match_id)
    view.rebuild()
    embed = build_pickban_embed(match)

    pb_msg = await channel.send(
        f"<@{t1_captain_id}> <@{t2_captain_id}> — Map selection has begun!\n"
        f"**{found_t1['team_name']}** (upper seed) bans first.",
        embed=embed,
        view=view,
    )
    await pb_msg.pin()
    return channel


# ── /bracket command ──────────────────────────────────────────────────────────
@bot.tree.command(
    name="bracket",
    description="Get the admin bracket panel link to start matches.",
    guild=discord.Object(id=SERVER_ID),
)
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


# ── Run ────────────────────────────────────────────────────────────────────────
async def main():
    import os
    TOKEN = os.environ.get("DISCORD_TOKEN")
    async with bot:
        await start_webhook_server()
        await bot.start(TOKEN)

asyncio.run(main())