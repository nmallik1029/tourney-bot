import uuid
import urllib.parse
import discord
from core.bot_instance import bot
from core.config import MAPS, RAILWAY_BASE, SERVER_ID, ROLE_ID
from core.storage import tournaments, active_matches

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
        title=f"{team1['name']} vs {team2['name']} -- {fmt} Map Selection",
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
        other_team = match["teams"][1 - team_idx]
        action_str = "BAN" if action == "ban" else "PICK"
        embed.add_field(
            name="Current Turn",
            value=f"**{current_team['name']}** -- {action_str} a map\n*{other_team['name']}, sit tight.*",
            inline=False,
        )
        embed.set_footer(text="Captains: use the buttons below  |  Next up: Hosting")
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

        if next_step is not None:
            view = PickBanView(match_id=self.match_id)
            view.rebuild()
            embed = build_pickban_embed(match)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            # Pick/ban done -- remove buttons from the pinned message
            embed = build_pickban_embed(match)
            await interaction.response.edit_message(embed=embed, view=None)

            decider = match["remaining_maps"][0] if match["remaining_maps"] else None
            all_maps = match["picked_maps"] + ([decider] if decider else [])
            maps_str = "\n".join(f"{i+1}. {m}" for i, m in enumerate(all_maps))

            # Higher seed (team index 0) always hosts
            host_captain_id = match["teams"][0]["captain_id"]
            match["host_captain_id"] = host_captain_id
            match["current_map_index"] = 0
            match["all_maps"] = all_maps

            host_embed = discord.Embed(
                title="Step 2/3 -- Host the Match",
                description=(
                    f"**Maps:**\n{maps_str}\n\n"
                    f"<@{host_captain_id}>, you're the host (upper seed).\n"
                    f"**Click the button below to host Map 1.**"
                ),
                color=0xFFA500,
            )
            host_embed.set_footer(text="Everyone else: hang tight until the game link is posted.")

            host_view = PickBanView(match_id=self.match_id)
            host_view.rebuild()
            host_msg = await interaction.channel.send(
                f"<@{host_captain_id}>",
                embed=host_embed,
                view=host_view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            match["host_message_id"] = host_msg.id


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
            f"**Hosting Map {self.map_index + 1}: {self.map_name}**\n\n"
            f"1. Click a button below to open your client\n"
            f"2. The game will auto-create\n"
            f"3. Paste the Krunker link back in this channel\n\n"
            f"Make sure your region is set to **New York**.",
            view=view,
            ephemeral=True,
        )

        match["current_map_index"] = self.map_index + 1
        match["host_button_used"] = True
        match.pop("host_message_id", None)

        # Visible status message so everyone knows hosting is in progress
        await interaction.channel.send(
            embed=discord.Embed(
                description=f"<@{interaction.user.id}> is hosting **Map {self.map_index + 1}: {self.map_name}**...\nWaiting for the game link.",
                color=0x2B2D31,
            )
        )
        view2 = PickBanView(match_id=self.match_id)
        view2.rebuild()
        await interaction.message.edit(view=view2)


# ── Match creation helper ─────────────────────────────────────────────────────
async def create_match(guild: discord.Guild, found_t1: dict, found_t2: dict, found_tournament: dict, fmt: str, challonge_match_id: int = None, casted: bool = False) -> discord.TextChannel:
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
        "series_score": {},
        "casted": casted,
    }
    active_matches[match_id] = match

    view = PickBanView(match_id=match_id)
    view.rebuild()
    embed = build_pickban_embed(match)

    pb_msg = await channel.send(
        f"<@{t1_captain_id}> <@{t2_captain_id}>\n"
        f"**Step 1/3 -- Map Selection**\n"
        f"**{found_t1['team_name']}** (upper seed) bans first.",
        embed=embed,
        view=view,
    )
    await pb_msg.pin()
    match["pickban_message_id"] = pb_msg.id
    return channel
