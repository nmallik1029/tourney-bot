import discord
from discord import app_commands
from discord.ext import commands
import uuid
import json
import re
import random
import asyncio
import os
import io
import urllib.parse
from pathlib import Path
import aiohttp
from aiohttp import web

# ── Bot setup ──────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ── Persistent storage (GitHub Gist) ──────────────────────────────────────────
GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
GIST_ID = os.environ.get("GITHUB_GIST_ID", "")
TOURNAMENTS_FILE = Path("tournaments.json")  # local fallback


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def load_tournaments() -> dict:
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                gist = json.loads(resp.read().decode())
                content = gist["files"]["tournaments.json"]["content"]
                data = json.loads(content)
                print(f"[Storage] Loaded {len(data)} tournaments from Gist")
                return data
        except Exception as e:
            print(f"[Storage] Failed to load from Gist: {e}")
    # Fallback to local file
    if TOURNAMENTS_FILE.exists():
        with open(TOURNAMENTS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_tournaments():
    data_str = json.dumps(tournaments, indent=2, default=str)
    # Save locally as backup
    with open(TOURNAMENTS_FILE, "w") as f:
        f.write(data_str)
    # Save to Gist
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            payload = json.dumps({"files": {"tournaments.json": {"content": data_str}}}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=payload,
                headers={**_gist_headers(), "Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"[Storage] Failed to save to Gist: {e}")


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

ROLE_ID = 1492368556627591248  # Role required for bot commands and staff channel access
SERVER_ID = 1481881771736956938


# ── Global command permission check ──────────────────────────────────────────
def is_authorized():
    """Check that user has ROLE_ID or is an administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        if any(r.id == ROLE_ID for r in interaction.user.roles):
            return True
        await interaction.response.send_message("You don't have permission to use bot commands.", ephemeral=True)
        return False
    return app_commands.check(predicate)


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
            "organizer_name": interaction.user.display_name,
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
                f"{link}\n\n"
                f"**{team1['name']}** (Team 1 / higher seed) join **Alpha**.\n"
                f"**{team2['name']}** (Team 2 / lower seed) join **Beta**.\n\n"
                f"{all_pings}"
            )
            return


# ── /tournament-start ──────────────────────────────────────────────────────────
    # Old Discord-based seeding removed — now handled via web seeding page


@bot.tree.command(name="tournament-start", description="Close sign-ups and start the tournament.", guild=discord.Object(id=SERVER_ID))
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

    # Generate admin token early so we can send the seeding URL
    if not t.get("admin_token"):
        t["admin_token"] = str(uuid.uuid4())
        save_tournaments()

    seeding_url = f"{RAILWAY_BASE}/admin/{t_id}/seeding?token={t['admin_token']}"
    await interaction.response.send_message(
        f"**Open the seeding page to reorder teams and start the bracket:**\n{seeding_url}",
        ephemeral=True,
    )


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
@is_authorized()
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
@is_authorized()
async def tournament_create(interaction: discord.Interaction):
    await interaction.response.send_modal(TournamentCreateModal())


# ── /tournament-info ──────────────────────────────────────────────────────────
_dashboard_tokens: set[str] = set()

@bot.tree.command(name="tournament-info", description="Open the tournament dashboard.", guild=discord.Object(id=SERVER_ID))
@is_authorized()
async def tournament_info(interaction: discord.Interaction):
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if not dashboard_token:
        dashboard_token = str(uuid.uuid4())
        _dashboard_tokens.add(dashboard_token)

    url = f"{RAILWAY_BASE}/dashboard?token={dashboard_token}"
    await interaction.response.send_message(f"**Tournament Dashboard:**\n{url}", ephemeral=True)


# ── /tournament-delete ─────────────────────────────────────────────────────────
# ── /tournament-end ───────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-end", description="End a tournament — clean up channels/roles but keep results.", guild=discord.Object(id=SERVER_ID))
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

    # Also delete any channels in the Matchrooms category (catches channels surviving restarts)
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

    # Delete tournament channels (signups, updates, admin)
    for key in ["signups_channel_id", "updates_channel_id", "admin_channel_id"]:
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

    # Mark tournament as ended but keep the data
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
        f"Results are preserved — view them on the dashboard or Challonge: {t.get('challonge_url', 'N/A')}"
    )


# ── /tournament-delete ─────────────────────────────────────────────────────────
@bot.tree.command(name="tournament-delete", description="Delete a tournament and its channels.", guild=discord.Object(id=SERVER_ID))
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

    # Delete main tournament channels
    for key in ["signups_channel_id", "updates_channel_id", "matches_channel_id", "admin_channel_id", "bracket_channel_id"]:
        ch_id = t.get(key)
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.delete()
                except Exception:
                    pass

    # Delete match room channels and clean up active matches
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

    del tournaments[t_id]
    save_tournaments()
    await interaction.followup.send(f"Tournament `{t_id}` has been deleted.")



# ── Error handler ─────────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        # Already handled by the check itself
        return
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
    img_buf = None
    image_file = None
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

    # Track series score and report to Challonge when series is decided
    active_match = next(
        (m for m in active_matches.values()
         if m.get("challonge_match_id")
         and {m["teams"][0]["name"].lower(), m["teams"][1]["name"].lower()}
         == {winner_discord["team_name"].lower(), loser_discord["team_name"].lower()}),
        None,
    )

    if active_match:
        # Store scoreboard image for this map
        if "scoreboard_images" not in active_match:
            active_match["scoreboard_images"] = []
        if img_buf:
            img_buf.seek(0)
            active_match["scoreboard_images"].append(img_buf.read())

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
        match_ch = bot.get_channel(active_match.get("channel_id"))
        if match_ch:
            if winner_series_wins >= wins_needed:
                await match_ch.send(
                    f"**{winner_discord['team_name']}** wins the series **{winner_series_wins}-{loser_series_wins}**! 🎉"
                )
            else:
                # Reply to the pick/ban message so the host can find the host button
                pb_msg_id = active_match.get("pickban_message_id")
                reference = discord.MessageReference(message_id=pb_msg_id, channel_id=match_ch.id) if pb_msg_id else None
                host_id = active_match.get("host_captain_id")
                host_ping = f"<@{host_id}>" if host_id else ""
                await match_ch.send(
                    f"**{winner_discord['team_name']}** wins this map! "
                    f"Series: **{winner_series_wins}-{loser_series_wins}**\n\n"
                    f"{host_ping} Host the next map! Use the host button above.",
                    reference=reference,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )

        # When the series is decided, post all scoreboards together
        if winner_series_wins >= wins_needed:
            # Determine overall series winner/loser names
            # The team whose key matches winner_key is the series winner
            t1_name = active_match["teams"][0]["name"]
            t2_name = active_match["teams"][1]["name"]
            if t1_name.lower() == winner_key:
                series_winner = t1_name
                series_loser = t2_name
            else:
                series_winner = t2_name
                series_loser = t1_name

            updates_ch = bot.get_channel(active_match.get("updates_channel_id"))
            if updates_ch:
                stored_images = active_match.get("scoreboard_images", [])
                if stored_images:
                    files = []
                    for i, img_bytes in enumerate(stored_images):
                        buf = io.BytesIO(img_bytes)
                        buf.seek(0)
                        files.append(discord.File(buf, filename=f"map{i+1}.png"))
                    await updates_ch.send(
                        f"**{series_winner}** won **{winner_series_wins}-{loser_series_wins}** against **{series_loser}**",
                        files=files,
                    )
                else:
                    await updates_ch.send(
                        f"**{series_winner}** won **{winner_series_wins}-{loser_series_wins}** against **{series_loser}**"
                    )
                print(f"[Webhook] Posted series result: {series_winner} {winner_series_wins}-{loser_series_wins} {series_loser}")

            # Report to Challonge
            challonge_id = active_match.get("challonge_id")
            ch_match_id = active_match.get("challonge_match_id")
            participant_map = active_match.get("challonge_participant_map", {})
            winner_ch_pid = participant_map.get(winner_discord.get("team_id"))

            if challonge_id and ch_match_id and winner_ch_pid:
                try:
                    open_matches = await challonge_get_matches(challonge_id, state="all")
                    ch_match = next((m for m in open_matches if m["id"] == ch_match_id), None)
                    if ch_match:
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
    else:
        # No active match found (standalone game) — post immediately
        updates_ch_id = matched_tournament.get("updates_channel_id")
        if updates_ch_id:
            updates_ch = bot.get_channel(updates_ch_id)
            if updates_ch:
                if image_file:
                    await updates_ch.send(
                        f"**{winner_discord['team_name']}** won **{score_str}** against **{loser_discord['team_name']}**",
                        file=image_file,
                    )
                else:
                    await updates_ch.send(
                        f"**{winner_discord['team_name']}** won **{score_str}** against **{loser_discord['team_name']}**"
                    )

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
  body {
    background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh;
  }

  /* ── Header ─────────────────────────────────────────────── */
  .header {
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border-bottom: 1px solid #21262d; padding: 28px 32px;
  }
  .header h1 { font-size: 1.6rem; font-weight: 600; }
  .header-meta { display: flex; gap: 16px; align-items: center; margin-top: 6px; }
  .header-meta a {
    color: #f09030; text-decoration: none; font-size: 0.85rem;
    transition: color 0.2s;
  }
  .header-meta a:hover { color: #ffa850; text-decoration: underline; }
  .header-meta span { color: #484f58; font-size: 0.8rem; }
  .refresh-dot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: #3fb950; margin-right: 4px; animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  .container { max-width: 1400px; margin: 0 auto; padding: 0 32px 32px; }

  /* ── Section labels ─────────────────────────────────────── */
  .section-label {
    font-size: 0.8rem; font-weight: 700; color: #f09030; text-transform: uppercase;
    letter-spacing: 1.5px; margin: 28px 0 8px; padding: 6px 12px;
    border-left: 3px solid #f09030; background: rgba(240,144,48,0.04);
    border-radius: 0 4px 4px 0;
    animation: fadeSlideIn 0.4s ease;
  }
  @keyframes fadeSlideIn {
    from { opacity: 0; transform: translateX(-10px); }
    to { opacity: 1; transform: translateX(0); }
  }

  /* ── Bracket layout ─────────────────────────────────────── */
  .bracket-wrapper {
    overflow-x: auto; padding: 8px 0 16px;
    animation: fadeIn 0.5s ease;
  }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  .bracket {
    display: flex; align-items: stretch; position: relative;
    width: max-content;
  }
  .round {
    display: flex; flex-direction: column; justify-content: center;
    min-width: 220px; position: relative; z-index: 1;
  }
  .round-hdr {
    text-align: center; font-size: 0.65rem; color: #8b949e; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1px; padding-bottom: 8px;
  }
  .round-matches { display: flex; flex-direction: column; justify-content: space-around; flex: 1; }

  /* Connector column between rounds */
  .connector-col { width: 32px; position: relative; flex-shrink: 0; }
  .connector-col svg { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
  .connector-col svg line { stroke: #21262d; stroke-width: 1.5; }

  /* ── Match card ─────────────────────────────────────────── */
  .match-card {
    background: #161b22; border: 1px solid #21262d; border-radius: 6px;
    width: 200px; margin: 4px auto; cursor: default;
    transition: all 0.25s ease; position: relative; overflow: hidden;
  }
  .match-card.open {
    border-color: #f09030; cursor: pointer;
    box-shadow: 0 0 0 1px rgba(240,144,48,0.1);
  }
  .match-card.open:hover {
    background: #1c2333; box-shadow: 0 4px 20px rgba(240,144,48,0.15);
    transform: translateY(-2px);
  }
  .match-card.started {
    border-color: #3fb950;
    box-shadow: 0 0 0 1px rgba(63,185,80,0.1);
  }
  .match-card.complete { opacity: 0.5; }
  .match-card.complete:hover { opacity: 0.7; }
  .match-id {
    position: absolute; top: 2px; right: 6px; font-size: 0.6rem; color: #484f58;
    font-weight: 600;
  }
  .team-row {
    display: flex; align-items: center; padding: 6px 10px; font-size: 0.8rem;
    transition: background 0.2s;
  }
  .team-row:last-of-type { border-bottom: none; }
  .team-row.top { border-bottom: 1px solid #21262d; }
  .team-name {
    flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    transition: color 0.2s;
  }
  .team-name.winner { color: #f09030; font-weight: 700; }
  .team-name.loser { color: #484f58; }
  .team-name.tbd { color: #30363d; font-style: italic; }
  .team-score {
    font-weight: 700; min-width: 24px; text-align: center; padding: 2px 6px;
    border-radius: 3px; font-size: 0.75rem; margin-left: 4px;
    transition: all 0.2s;
  }
  .team-score.win { background: #f09030; color: #000; }
  .match-status {
    font-size: 0.6rem; text-align: center; padding: 3px; text-transform: uppercase;
    letter-spacing: 0.5px; font-weight: 600; transition: all 0.2s;
  }
  .match-status.live {
    background: rgba(63,185,80,0.15); color: #3fb950;
    animation: liveGlow 2s infinite;
  }
  @keyframes liveGlow {
    0%, 100% { background: rgba(63,185,80,0.15); }
    50% { background: rgba(63,185,80,0.25); }
  }
  .match-status.open-status { background: rgba(240,144,48,0.1); color: #f09030; }

  /* ── Format picker modal ────────────────────────────────── */
  .modal-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0); z-index: 100; align-items: center; justify-content: center;
    transition: background 0.3s ease;
  }
  .modal-overlay.active { display: flex; background: rgba(0,0,0,0.7); }
  .modal {
    background: #161b22; border: 1px solid #f09030; border-radius: 12px;
    padding: 28px; min-width: 340px; text-align: center;
    transform: scale(0.9); opacity: 0;
    transition: all 0.3s ease;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  .modal-overlay.active .modal { transform: scale(1); opacity: 1; }
  .modal h2 { font-size: 1.1rem; margin-bottom: 4px; font-weight: 600; }
  .modal .match-label { color: #8b949e; font-size: 0.85rem; margin-bottom: 20px; }
  .modal .fmt-buttons { display: flex; gap: 10px; justify-content: center; margin-bottom: 20px; }
  .modal .fmt-btn {
    padding: 10px 24px; border-radius: 8px; border: 2px solid #21262d;
    background: #0d1117; color: #e6edf3; font-size: 1rem; cursor: pointer;
    transition: all 0.25s ease; font-weight: 500;
  }
  .modal .fmt-btn:hover { border-color: #f09030; background: #161b22; transform: translateY(-1px); }
  .modal .fmt-btn.selected { border-color: #f09030; background: #f09030; color: #000; font-weight: 700; }
  .modal .start-btn {
    padding: 10px 36px; border-radius: 8px; border: none;
    background: #3fb950; color: #fff; font-size: 1rem; font-weight: 700;
    cursor: pointer; transition: all 0.25s ease;
  }
  .modal .start-btn:hover { background: #2ea043; transform: translateY(-1px); box-shadow: 0 4px 12px rgba(63,185,80,0.3); }
  .modal .start-btn:disabled { background: #21262d; color: #484f58; cursor: not-allowed; transform: none; box-shadow: none; }
  .modal .cancel-btn {
    padding: 8px 20px; border-radius: 6px; border: 1px solid #30363d;
    background: transparent; color: #8b949e; font-size: 0.85rem;
    cursor: pointer; margin-top: 12px; transition: all 0.2s ease;
  }
  .modal .cancel-btn:hover { border-color: #484f58; color: #e6edf3; }

  /* ── Loading state ──────────────────────────────────────── */
  .loading-text { color: #8b949e; padding: 40px; text-align: center; }
</style>
</head>
<body>
<div class="header">
  <h1 id="title">Loading...</h1>
  <div class="header-meta" id="header-meta"></div>
</div>
<div class="container">

<div class="section-label">Winners Bracket</div>
<div class="bracket-wrapper"><div class="bracket" id="winners-bracket"></div></div>

<div class="section-label">Losers Bracket</div>
<div class="bracket-wrapper"><div class="bracket" id="losers-bracket"></div></div>

<div class="section-label" id="gf-label">Grand Finals</div>
<div class="bracket-wrapper"><div class="bracket" id="grand-finals"></div></div>

</div>

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
  document.getElementById("header-meta").innerHTML =
    '<a href="' + data.challonge_url + '" target="_blank">View on Challonge &#8599;</a>' +
    '<span><span class="refresh-dot"></span>Live &mdash; refreshes every 30s</span>';

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


SEEDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Seeding — Tournament Admin</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh;
  }

  .header {
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border-bottom: 1px solid #21262d; padding: 28px 32px;
  }
  .header h1 { font-size: 1.6rem; font-weight: 600; }
  .header p { color: #8b949e; font-size: 0.85rem; margin-top: 4px; }

  .container { max-width: 560px; margin: 0 auto; padding: 24px 32px; }

  .seeding-list { display: flex; flex-direction: column; gap: 6px; }
  .seed-item {
    display: flex; align-items: center; gap: 12px;
    background: #161b22; border: 1px solid #21262d; border-radius: 8px;
    padding: 12px 16px; cursor: grab; user-select: none;
    transition: all 0.25s ease;
  }
  .seed-item:hover { border-color: #30363d; background: #1c2333; }
  .seed-item:active { cursor: grabbing; }
  .seed-item.dragging { opacity: 0.35; transform: scale(0.96); }
  .seed-item.drag-over { border-color: #f09030; background: #1c2333; box-shadow: 0 0 12px rgba(240,144,48,0.1); }
  .seed-num {
    font-size: 1rem; font-weight: 700; color: #f09030; min-width: 28px;
    text-align: center; background: rgba(240,144,48,0.1); border-radius: 6px;
    padding: 2px 0;
  }
  .seed-name { flex: 1; font-size: 0.95rem; position: relative; font-weight: 500; }
  .seed-handle { color: #30363d; font-size: 1.1rem; cursor: grab; transition: color 0.2s; }
  .seed-item:hover .seed-handle { color: #484f58; }
  .seed-roster {
    display: none; position: absolute; left: 100%; top: 50%; transform: translateY(-50%);
    margin-left: 14px; background: #161b22; border: 1px solid #f09030; border-radius: 8px;
    padding: 10px 16px; white-space: nowrap; z-index: 10; font-size: 0.8rem;
    color: #8b949e; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    animation: tooltipIn 0.15s ease;
  }
  @keyframes tooltipIn { from { opacity: 0; transform: translateY(-50%) translateX(-4px); } to { opacity: 1; transform: translateY(-50%) translateX(0); } }
  .seed-roster .roster-title { color: #f09030; font-weight: 700; margin-bottom: 6px; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px; }
  .seed-roster .roster-player { padding: 2px 0; color: #e6edf3; }
  .seed-name:hover .seed-roster { display: block; }

  .seed-arrows { display: flex; flex-direction: column; gap: 3px; }
  .seed-arrows button {
    background: #0d1117; border: 1px solid #21262d; color: #484f58; width: 28px; height: 24px;
    border-radius: 4px; cursor: pointer; font-size: 0.7rem; display: flex; align-items: center;
    justify-content: center; transition: all 0.2s ease;
  }
  .seed-arrows button:hover { border-color: #f09030; color: #f09030; background: #161b22; }
  .seed-arrows button:disabled { opacity: 0.3; cursor: default; }

  .confirm-area { margin: 28px 0 0; text-align: center; }
  .confirm-btn {
    padding: 12px 44px; border-radius: 8px; border: none;
    background: #3fb950; color: #fff; font-size: 1.05rem; font-weight: 700;
    cursor: pointer; transition: all 0.25s ease;
  }
  .confirm-btn:hover { background: #2ea043; transform: translateY(-1px); box-shadow: 0 4px 16px rgba(63,185,80,0.3); }
  .confirm-btn:disabled { background: #21262d; color: #484f58; cursor: not-allowed; transform: none; box-shadow: none; }
  .confirm-status { margin-top: 14px; color: #8b949e; font-size: 0.85rem; transition: color 0.3s; }
  .confirm-status.error { color: #f85149; }
</style>
</head>
<body>
<div class="header">
  <h1 id="title">Loading...</h1>
  <p>Drag teams to reorder seeding, then confirm to create the bracket.</p>
</div>
<div class="container">

<div class="seeding-list" id="seeding-list"></div>

<div class="confirm-area">
  <button class="confirm-btn" id="confirm-btn" disabled>Confirm &amp; Create Bracket</button>
  <p class="confirm-status" id="confirm-status"></p>
</div>

</div>

<script>
const T_ID = "{{TOURNAMENT_ID}}";
const TOKEN = "{{TOKEN}}";
let teams = [];
let dragIdx = null;

async function loadTeams() {
  const resp = await fetch(`/api/seeding/${T_ID}?token=${TOKEN}`);
  if (!resp.ok) { document.getElementById("title").textContent = "Error loading teams"; return; }
  const data = await resp.json();
  document.getElementById("title").textContent = data.name + " — Seeding";
  teams = data.teams;
  renderList();
  document.getElementById("confirm-btn").disabled = false;
}

function renderList() {
  const list = document.getElementById("seeding-list");
  list.innerHTML = "";
  teams.forEach((t, i) => {
    const item = document.createElement("div");
    item.className = "seed-item";
    item.draggable = true;
    item.dataset.index = i;

    const players = t.players || [];
    let rosterHtml = '<div class="seed-roster"><div class="roster-title">Players</div>';
    players.forEach(p => { rosterHtml += '<div class="roster-player">' + esc(p) + '</div>'; });
    rosterHtml += '</div>';

    item.innerHTML =
      '<span class="seed-handle">&#9776;</span>' +
      '<span class="seed-num">' + (i + 1) + '</span>' +
      '<div style="flex:1"><div class="seed-name">' + esc(t.team_name) + rosterHtml + '</div></div>' +
      '<div class="seed-arrows">' +
        '<button data-dir="up" data-idx="' + i + '"' + (i === 0 ? ' disabled' : '') + '>&#9650;</button>' +
        '<button data-dir="down" data-idx="' + i + '"' + (i === teams.length - 1 ? ' disabled' : '') + '>&#9660;</button>' +
      '</div>';

    // Drag events
    item.addEventListener("dragstart", e => { dragIdx = i; item.classList.add("dragging"); });
    item.addEventListener("dragend", () => { dragIdx = null; document.querySelectorAll(".seed-item").forEach(el => el.classList.remove("dragging", "drag-over")); });
    item.addEventListener("dragover", e => { e.preventDefault(); item.classList.add("drag-over"); });
    item.addEventListener("dragleave", () => item.classList.remove("drag-over"));
    item.addEventListener("drop", e => {
      e.preventDefault();
      item.classList.remove("drag-over");
      if (dragIdx !== null && dragIdx !== i) {
        const moved = teams.splice(dragIdx, 1)[0];
        teams.splice(i, 0, moved);
        renderList();
      }
    });

    // Arrow button events
    item.querySelectorAll(".seed-arrows button").forEach(btn => {
      btn.addEventListener("click", e => {
        e.stopPropagation();
        const idx = parseInt(btn.dataset.idx);
        const dir = btn.dataset.dir;
        if (dir === "up" && idx > 0) {
          [teams[idx], teams[idx - 1]] = [teams[idx - 1], teams[idx]];
        } else if (dir === "down" && idx < teams.length - 1) {
          [teams[idx], teams[idx + 1]] = [teams[idx + 1], teams[idx]];
        }
        renderList();
      });
    });

    list.appendChild(item);
  });
}

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

document.getElementById("confirm-btn").addEventListener("click", async () => {
  const btn = document.getElementById("confirm-btn");
  const status = document.getElementById("confirm-status");
  btn.disabled = true;
  btn.textContent = "Creating bracket...";
  status.textContent = "Setting up channels, Challonge bracket, and participants...";
  status.className = "confirm-status";

  try {
    const teamOrder = teams.map(t => t.team_id);
    const resp = await fetch("/api/confirm-seeding", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ tournament_id: T_ID, token: TOKEN, team_order: teamOrder }),
    });
    const data = await resp.json();
    if (data.ok) {
      status.textContent = "Bracket created! Redirecting...";
      setTimeout(() => {
        window.location.href = `/admin/${T_ID}?token=${TOKEN}`;
      }, 1500);
    } else {
      status.textContent = "Error: " + (data.error || "Unknown error");
      status.className = "confirm-status error";
      btn.textContent = "Confirm & Create Bracket";
      btn.disabled = false;
    }
  } catch(e) {
    status.textContent = "Error: " + e.message;
    status.className = "confirm-status error";
    btn.textContent = "Confirm & Create Bracket";
    btn.disabled = false;
  }
});

loadTeams();
</script>
</body>
</html>"""


async def handle_admin_seeding(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")
    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.Response(status=403, text="Invalid tournament or token.")
    if t.get("challonge_id"):
        # Already started — redirect to bracket
        raise web.HTTPFound(f"/admin/{t_id}?token={token}")
    html = SEEDING_HTML.replace("{{TOURNAMENT_ID}}", t_id).replace("{{TOKEN}}", token)
    return web.Response(content_type="text/html", text=html)


async def handle_api_seeding(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")
    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    team_list = []
    for tm in t.get("teams", []):
        igns = [p.get("ign", "Unknown") for p in tm.get("players", [])]
        team_list.append({
            "team_id": tm["team_id"],
            "team_name": tm["team_name"],
            "players": igns,
        })
    return web.json_response({"name": t["name"], "teams": team_list})


async def handle_api_confirm_seeding(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    t_id = body.get("tournament_id", "").upper()
    token = body.get("token", "")
    team_order = body.get("team_order", [])

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    if t.get("challonge_id"):
        return web.json_response({"error": "bracket already created"}, status=400)

    # Reorder teams based on the submitted order
    team_map = {tm["team_id"]: tm for tm in t.get("teams", [])}
    seeded_teams = []
    for tid in team_order:
        if tid in team_map:
            seeded_teams.append(team_map[tid])
    # Append any teams not in the order (safety net)
    for tm in t.get("teams", []):
        if tm["team_id"] not in [st["team_id"] for st in seeded_teams]:
            seeded_teams.append(tm)

    t["open"] = False
    t["teams"] = seeded_teams
    save_tournaments()

    # Create channels
    guild = bot.get_guild(SERVER_ID)
    if not guild:
        return web.json_response({"error": "bot not in guild"}, status=500)

    staff_role = guild.get_role(ROLE_ID)
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

    try:
        updates_channel = await guild.create_text_channel(name="tournament-updates", overwrites=read_only, category=t_cat)
        admin_channel = await guild.create_text_channel(name="tournament-admin", overwrites=admin_perms, category=a_cat)
        t["updates_channel_id"] = updates_channel.id
        t["admin_channel_id"] = admin_channel.id
        save_tournaments()
    except Exception as e:
        return web.json_response({"error": f"channel creation failed: {e}"}, status=500)

    # Create per-team roles, categories, and channels (text + vc)
    t["team_roles"] = {}
    t["team_channels"] = {}
    t["team_categories"] = {}
    for tm in seeded_teams:
        team_name = tm["team_name"]
        try:
            role = await guild.create_role(name=team_name, mentionable=True)
            tm["role_id"] = role.id
            t["team_roles"][tm["team_id"]] = role.id

            # Assign role to team members using players list
            for player in tm.get("players", []):
                pid = player.get("discord_id")
                if pid:
                    member = guild.get_member(pid)
                    if member:
                        try:
                            await member.add_roles(role)
                            print(f"[Teams] Assigned role '{team_name}' to {member.display_name}")
                        except Exception as e:
                            print(f"[Teams] Failed to assign role to {pid}: {e}")

            # Category + channel perms: only this team + staff + bot can see
            team_perms = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True),
            }
            if staff_role:
                team_perms[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True)

            team_category = await guild.create_category(name=team_name, overwrites=team_perms)
            t["team_categories"][tm["team_id"]] = team_category.id

            text_ch = await guild.create_text_channel(name=team_name, category=team_category)
            voice_ch = await guild.create_voice_channel(name=team_name, category=team_category)
            t["team_channels"][tm["team_id"]] = {"text": text_ch.id, "voice": voice_ch.id}
            print(f"[Teams] Created role + category + channels for {team_name}")
        except Exception as e:
            print(f"[Teams] Error creating role/channels for {team_name}: {e}")
    save_tournaments()

    # Create Challonge bracket
    team_count = len(seeded_teams)
    ch_name = f"krunkerbiweekly-{t['name']}"
    slug = re.sub(r"[^a-z0-9]", "_", ch_name.lower()).strip("_")[:60]
    try:
        print(f"[Challonge] Creating tournament: {ch_name} ({slug})")
        ch_tourney = await challonge_create_tournament(ch_name, slug)
        challonge_id = ch_tourney["id"]
        t["challonge_id"] = challonge_id
        t["challonge_url"] = ch_tourney.get("full_challonge_url", f"https://krunker-biweeklies.challonge.com/{slug}")
        print(f"[Challonge] Tournament created: {challonge_id}")

        participants = [
            {"name": tm["team_name"], "seed": i + 1, "misc": tm["team_id"]}
            for i, tm in enumerate(seeded_teams)
        ]
        print(f"[Challonge] Adding {len(participants)} participants...")
        added = await challonge_add_participants(challonge_id, participants)
        print(f"[Challonge] Participants added: {len(added)}")

        participant_map = {}
        if isinstance(added, list):
            for entry in added:
                p = entry.get("participant", entry)
                if p.get("misc"):
                    participant_map[p["misc"]] = p["id"]
        t["challonge_participant_map"] = participant_map

        await challonge_start(challonge_id)
        save_tournaments()
        print(f"[Challonge] Tournament started successfully")

        bracket_msg = f"**{t['name']}** has started with {team_count} teams!\n**Bracket:** {t['challonge_url']}"
    except Exception as e:
        print(f"[Challonge] Error: {e}")
        import traceback
        traceback.print_exc()
        bracket_msg = f"**{t['name']}** has started with {team_count} teams.\n⚠️ Challonge bracket creation failed: {e}"

    # Post to Discord channels
    signups_ch = guild.get_channel(t.get("signups_channel_id"))
    if signups_ch:
        await signups_ch.send(f"Sign-ups closed — {team_count} team{'s' if team_count != 1 else ''} registered.")

    updates_ch = guild.get_channel(t.get("updates_channel_id"))
    if updates_ch:
        await updates_ch.send(bracket_msg)

    return web.json_response({"ok": True})


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tournament Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border-bottom: 1px solid #21262d; padding: 28px 32px;
  }
  .header h1 { font-size: 1.6rem; font-weight: 600; }
  .header p { color: #8b949e; font-size: 0.85rem; margin-top: 4px; }
  .container { max-width: 1100px; margin: 0 auto; padding: 24px 32px; }

  /* Tournament list */
  .tournament-list { display: flex; flex-direction: column; gap: 12px; }
  .t-card {
    background: #161b22; border: 1px solid #21262d; border-radius: 8px;
    overflow: hidden; cursor: pointer; transition: all 0.25s ease;
  }
  .t-card:hover { border-color: #f09030; transform: translateY(-2px); box-shadow: 0 4px 20px rgba(240,144,48,0.1); }
  .t-card-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 20px; gap: 16px;
  }
  .t-card-title { font-size: 1.05rem; font-weight: 600; }
  .t-card-meta { display: flex; gap: 12px; align-items: center; flex-shrink: 0; }
  .badge {
    padding: 3px 10px; border-radius: 12px; font-size: 0.7rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .badge.open { background: rgba(63,185,80,0.15); color: #3fb950; }
  .badge.started { background: rgba(240,144,48,0.15); color: #f09030; }
  .badge.ended { background: rgba(139,148,158,0.15); color: #8b949e; }
  .team-count { color: #8b949e; font-size: 0.8rem; }
  .chevron { color: #484f58; font-size: 1.2rem; transition: transform 0.3s ease; }
  .t-card.expanded .chevron { transform: rotate(90deg); }

  /* Expanded detail panel */
  .t-card-detail {
    max-height: 0; overflow: hidden; transition: max-height 0.4s ease, padding 0.3s ease;
    background: #0d1117; border-top: 1px solid #21262d;
  }
  .t-card.expanded .t-card-detail { max-height: 2000px; padding: 20px; }

  .detail-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
  }
  @media (max-width: 700px) { .detail-grid { grid-template-columns: 1fr; } }

  .detail-section {
    background: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 14px 16px;
  }
  .detail-section h3 {
    font-size: 0.75rem; color: #f09030; text-transform: uppercase; letter-spacing: 1px;
    margin-bottom: 10px; font-weight: 700;
  }
  .detail-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 5px 0; font-size: 0.85rem; border-bottom: 1px solid #21262d;
  }
  .detail-row:last-child { border-bottom: none; }
  .detail-label { color: #8b949e; }
  .detail-value { font-weight: 500; }
  .detail-value a { color: #f09030; text-decoration: none; }
  .detail-value a:hover { text-decoration: underline; }

  /* Team list inside detail */
  .team-list { list-style: none; }
  .team-list li {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 0.85rem;
  }
  .team-list li:last-child { border-bottom: none; }
  .team-seed-badge {
    background: #21262d; color: #f09030; font-weight: 700; font-size: 0.7rem;
    width: 24px; height: 24px; border-radius: 50%; display: flex;
    align-items: center; justify-content: center; flex-shrink: 0;
  }
  .team-players { color: #8b949e; font-size: 0.75rem; margin-left: auto; }

  /* Matches section */
  .match-item {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 0.85rem;
  }
  .match-item:last-child { border-bottom: none; }
  .match-vs { color: #484f58; font-size: 0.75rem; }
  .match-score { color: #f09030; font-weight: 700; margin-left: auto; }
  .match-live { color: #3fb950; font-weight: 600; font-size: 0.75rem; margin-left: auto; }

  .empty-state { color: #484f58; font-style: italic; font-size: 0.85rem; padding: 8px 0; }

  /* Action links */
  .actions { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
  .action-btn {
    display: inline-block; padding: 7px 16px; border-radius: 6px; font-size: 0.8rem;
    font-weight: 600; text-decoration: none; transition: all 0.2s ease;
    border: 1px solid #21262d; color: #e6edf3; background: #161b22;
  }
  .action-btn:hover { border-color: #f09030; color: #f09030; }
  .action-btn.primary { background: #f09030; color: #000; border-color: #f09030; }
  .action-btn.primary:hover { background: #d67d20; }

  .loading { text-align: center; padding: 60px; color: #8b949e; }
  .no-tournaments { text-align: center; padding: 80px 20px; color: #484f58; }
  .no-tournaments h2 { font-size: 1.2rem; margin-bottom: 8px; color: #8b949e; }
</style>
</head>
<body>
<div class="header">
  <h1>Tournament Dashboard</h1>
  <p>All tournaments at a glance. Click to expand.</p>
</div>
<div class="container">
  <div id="content" class="loading">Loading tournaments...</div>
</div>

<script>
const TOKEN = "{{TOKEN}}";

async function loadDashboard() {
  const resp = await fetch(`/api/dashboard?token=${TOKEN}`);
  if (!resp.ok) { document.getElementById("content").textContent = "Error loading data."; return; }
  const data = await resp.json();
  const container = document.getElementById("content");

  if (!data.tournaments || data.tournaments.length === 0) {
    container.innerHTML = '<div class="no-tournaments"><h2>No tournaments yet</h2><p>Create one with /tournament-create</p></div>';
    container.className = "";
    return;
  }

  container.className = "tournament-list";
  container.innerHTML = "";

  for (const t of data.tournaments) {
    const card = document.createElement("div");
    card.className = "t-card";

    const statusClass = t.ended ? "ended" : (t.challonge_id ? "started" : (t.open ? "open" : "ended"));
    const statusText = t.ended ? "Ended" : (t.open ? "Sign-ups Open" : (t.challonge_id ? "In Progress" : "Closed"));
    const teamCount = t.teams.length;

    // Header
    card.innerHTML =
      '<div class="t-card-header">' +
        '<span class="t-card-title">' + esc(t.name) + '</span>' +
        '<div class="t-card-meta">' +
          '<span class="team-count">' + teamCount + ' team' + (teamCount !== 1 ? 's' : '') + '</span>' +
          '<span class="badge ' + statusClass + '">' + statusText + '</span>' +
          '<span class="chevron">&#9656;</span>' +
        '</div>' +
      '</div>' +
      '<div class="t-card-detail">' + buildDetail(t) + '</div>';

    card.querySelector(".t-card-header").addEventListener("click", () => {
      card.classList.toggle("expanded");
    });

    container.appendChild(card);
  }
}

function buildDetail(t) {
  let html = '<div class="detail-grid">';

  // Info section
  html += '<div class="detail-section"><h3>Info</h3>';
  html += detailRow("Tournament ID", t.id);
  html += detailRow("Organizer", t.organizer_name || "Unknown");
  html += detailRow("Team Size", t.team_size + "v" + t.team_size);
  html += detailRow("Status", t.ended ? "Ended" : (t.open ? "Sign-ups Open" : (t.challonge_id ? "In Progress" : "Closed")));
  if (t.challonge_url) {
    html += detailRow("Bracket", '<a href="' + esc(t.challonge_url) + '" target="_blank">View on Challonge &#8599;</a>');
  }
  if (t.admin_panel_url) {
    html += detailRow("Admin Panel", '<a href="' + esc(t.admin_panel_url) + '" target="_blank">Open &#8599;</a>');
  }
  html += '</div>';

  // Teams section
  html += '<div class="detail-section"><h3>Teams (' + t.teams.length + ')</h3>';
  if (t.teams.length === 0) {
    html += '<div class="empty-state">No teams registered yet.</div>';
  } else {
    html += '<ul class="team-list">';
    t.teams.forEach((tm, i) => {
      const players = tm.players.map(p => esc(p)).join(", ");
      html += '<li>' +
        '<span class="team-seed-badge">' + (i + 1) + '</span>' +
        '<span>' + esc(tm.team_name) + '</span>' +
        '<span class="team-players">' + players + '</span>' +
      '</li>';
    });
    html += '</ul>';
  }
  html += '</div>';

  // Active matches section
  html += '<div class="detail-section" style="grid-column: 1 / -1;"><h3>Matches</h3>';
  if (!t.matches || t.matches.length === 0) {
    html += '<div class="empty-state">No matches started yet.</div>';
  } else {
    t.matches.forEach(m => {
      const t1 = m.team1 || "TBD";
      const t2 = m.team2 || "TBD";
      let scoreHtml = '';
      if (m.series_score) scoreHtml = '<span class="match-score">' + esc(m.series_score) + '</span>';
      else scoreHtml = '<span class="match-live">LIVE</span>';
      html += '<div class="match-item">' +
        '<span>' + esc(t1) + '</span>' +
        '<span class="match-vs">vs</span>' +
        '<span>' + esc(t2) + '</span>' +
        scoreHtml +
      '</div>';
    });
  }
  html += '</div>';

  html += '</div>'; // close detail-grid

  // Actions
  html += '<div class="actions">';
  if (t.challonge_url) {
    html += '<a class="action-btn primary" href="' + esc(t.challonge_url) + '" target="_blank">Challonge Bracket</a>';
  }
  if (t.admin_panel_url) {
    html += '<a class="action-btn" href="' + esc(t.admin_panel_url) + '" target="_blank">Admin Panel</a>';
  }
  html += '</div>';

  return html;
}

function detailRow(label, value) {
  return '<div class="detail-row"><span class="detail-label">' + label + '</span><span class="detail-value">' + value + '</span></div>';
}

function esc(s) { if (!s) return ""; const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

loadDashboard();
</script>
</body>
</html>"""


def _valid_dashboard_token(token: str) -> bool:
    if not token:
        return False
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if dashboard_token and token == dashboard_token:
        return True
    if token in _dashboard_tokens:
        return True
    return any(t.get("admin_token") == token for t in tournaments.values())


async def handle_dashboard(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not _valid_dashboard_token(token):
        return web.Response(status=403, text="Invalid token.")
    html = DASHBOARD_HTML.replace("{{TOKEN}}", token)
    return web.Response(content_type="text/html", text=html)


async def handle_api_dashboard(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not _valid_dashboard_token(token):
        return web.json_response({"error": "forbidden"}, status=403)

    result = []
    for t_id, t in tournaments.items():
        teams_data = []
        for tm in t.get("teams", []):
            igns = [p.get("ign", "Unknown") for p in tm.get("players", [])]
            teams_data.append({"team_name": tm["team_name"], "team_id": tm["team_id"], "players": igns})

        # Gather active matches for this tournament
        match_data = []
        for mid, m in active_matches.items():
            if m.get("tournament_id") == t_id:
                t1 = m["teams"][0]["name"] if m.get("teams") else "?"
                t2 = m["teams"][1]["name"] if m.get("teams") and len(m["teams"]) > 1 else "?"
                series = m.get("series_score", {})
                fmt = m.get("format", "bo1")
                if series:
                    s1 = series.get(t1.lower(), 0)
                    s2 = series.get(t2.lower(), 0)
                    score_str = f"{s1}-{s2}"
                else:
                    score_str = None
                match_data.append({"team1": t1, "team2": t2, "format": fmt, "series_score": score_str})

        admin_panel_url = None
        if t.get("admin_token") and t.get("challonge_id"):
            admin_panel_url = f"{RAILWAY_BASE}/admin/{t_id}?token={t['admin_token']}"

        result.append({
            "id": t_id,
            "name": t.get("name", t_id),
            "open": t.get("open", False),
            "ended": t.get("ended", False),
            "team_size": t.get("team_size", "?"),
            "organizer_name": t.get("organizer_name", "Unknown"),
            "challonge_id": t.get("challonge_id"),
            "challonge_url": t.get("challonge_url"),
            "admin_panel_url": admin_panel_url,
            "teams": teams_data,
            "matches": match_data,
        })

    return web.json_response({"tournaments": result})


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/krunker", handle_krunker_webhook)
    app.router.add_get("/launch", handle_launch)
    app.router.add_get("/admin/{tournament_id}/seeding", handle_admin_seeding)
    app.router.add_get("/admin/{tournament_id}", handle_admin_bracket)
    app.router.add_get("/api/seeding/{tournament_id}", handle_api_seeding)
    app.router.add_post("/api/confirm-seeding", handle_api_confirm_seeding)
    app.router.add_get("/api/tournament/{tournament_id}", handle_api_tournament)
    app.router.add_post("/api/start-match", handle_api_start_match)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/api/dashboard", handle_api_dashboard)
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
                f"<@{host_captain_id}> as the upper seed, you must **host**.\n"
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

    staff_role = guild.get_role(ROLE_ID)

    # Find or create the Matchrooms category
    match_cat = discord.utils.get(guild.categories, name="Matchrooms")
    if not match_cat:
        match_cat = await guild.create_category("Matchrooms")

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
        category=match_cat,
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
    match["pickban_message_id"] = pb_msg.id
    return channel


# ── /bracket command ──────────────────────────────────────────────────────────
@bot.tree.command(
    name="bracket",
    description="Get the admin bracket panel link to start matches.",
    guild=discord.Object(id=SERVER_ID),
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


# ── Run ────────────────────────────────────────────────────────────────────────
async def main():
    import os
    TOKEN = os.environ.get("DISCORD_TOKEN")
    async with bot:
        await start_webhook_server()
        await bot.start(TOKEN)

asyncio.run(main())