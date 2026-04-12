import uuid
import discord
from discord import app_commands
from core.bot_instance import bot
from core.config import MAPS, ROLE_ID, is_authorized, guild_object
from core.storage import tournaments, active_matches
from views.pickban import PickBanView, build_pickban_embed

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


@bot.tree.command(name="test-scoreboard", description="Post a test scoreboard to see how it looks.", guild=guild_object())
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


@bot.tree.command(
    name="test-match",
    description="Create a test match channel with full pick/ban flow.",
    guild=guild_object(),
)
@is_authorized()
@app_commands.describe(
    format="Match format (default: bo1)",
    casted="Whether this match is casted (default: No)",
)
@app_commands.choices(
    format=[
        app_commands.Choice(name="Best of 1", value="bo1"),
        app_commands.Choice(name="Best of 3", value="bo3"),
        app_commands.Choice(name="Best of 5", value="bo5"),
    ],
    casted=[
        app_commands.Choice(name="No", value="no"),
        app_commands.Choice(name="Yes", value="yes"),
    ],
)
async def test_match_cmd(
    interaction: discord.Interaction,
    format: app_commands.Choice[str] = None,
    casted: app_commands.Choice[str] = None,
):
    """Creates a test match channel with two fake teams and full pick/ban."""
    fmt = format.value if format else "bo1"
    is_casted = casted.value == "yes" if casted else False
    user_id = interaction.user.id
    guild = interaction.guild
    match_id = f"test-{str(uuid.uuid4())[:6]}"

    match_cat = discord.utils.get(guild.categories, name="Matchrooms")
    if not match_cat:
        match_cat = await guild.create_category("Matchrooms")

    staff_role = guild.get_role(ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    channel = await guild.create_text_channel(
        name=f"test-TeamAlpha vs TeamBeta",
        overwrites=overwrites,
        category=match_cat,
    )

    caster_channel_id = None
    if is_casted:
        caster_ch = discord.utils.get(guild.text_channels, name="test-caster-links")
        if not caster_ch:
            caster_ch = await guild.create_text_channel(
                name="test-caster-links",
                overwrites=overwrites,
                category=match_cat,
            )
        caster_channel_id = caster_ch.id
        tournaments["TEST"] = {
            "id": "TEST",
            "name": "Test Tournament",
            "caster_channel_id": caster_channel_id,
        }

    match = {
        "match_id": match_id,
        "format": fmt,
        "team_size": "4v4",
        "teams": [
            {"name": "TeamAlpha", "captain_id": user_id, "player_ids": [user_id]},
            {"name": "TeamBeta", "captain_id": user_id, "player_ids": [user_id]},
        ],
        "remaining_maps": list(MAPS),
        "picked_maps": [],
        "step": 0,
        "current_map_index": 0,
        "channel_id": channel.id,
        "tournament_id": "TEST",
        "updates_channel_id": channel.id,
        "challonge_match_id": None,
        "challonge_id": None,
        "challonge_participant_map": {},
        "series_score": {},
        "casted": is_casted,
    }
    active_matches[match_id] = match

    view = PickBanView(match_id=match_id)
    view.rebuild()
    embed = build_pickban_embed(match)

    pb_msg = await channel.send(
        f"<@{user_id}>\n"
        f"**Step 1/3 -- Map Selection**\n"
        f"**TeamAlpha** (upper seed) bans first.",
        embed=embed,
        view=view,
    )
    await pb_msg.pin()
    match["pickban_message_id"] = pb_msg.id

    casted_label = " | \U0001f3a5 **Casted**" if is_casted else ""
    await interaction.response.send_message(
        f"Test match created -> {channel.mention}\n"
        f"Format: **{fmt.upper()}**{casted_label} | You are captain of both teams.",
        ephemeral=True,
    )


@bot.tree.command(
    name="test-match-cleanup",
    description="Remove all test matches and their channels.",
    guild=guild_object(),
)
@is_authorized()
async def test_match_cleanup_cmd(interaction: discord.Interaction):
    removed = [
        mid for mid, m in list(active_matches.items())
        if mid.startswith("test-")
    ]
    deleted_channels = 0
    for mid in removed:
        ch = interaction.guild.get_channel(active_matches[mid].get("channel_id"))
        if ch:
            await ch.delete()
            deleted_channels += 1
        del active_matches[mid]

    caster_ch = discord.utils.get(interaction.guild.text_channels, name="test-caster-links")
    if caster_ch:
        await caster_ch.delete()
        deleted_channels += 1

    tournaments.pop("TEST", None)

    await interaction.response.send_message(
        f"Removed {len(removed)} test match(es) and {deleted_channels} channel(s)." if removed else "No test matches found.",
        ephemeral=True,
    )
