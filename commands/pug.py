import time

import discord
from discord import app_commands

from core.bot_instance import bot
from core.config import guild_object
from pug.config import (
    is_pug_admin,
    is_pug_staff,
    ELO_START,
    VALID_REGIONS,
    QUEUE_CHANNEL_NAME,
    RESULTS_CHANNEL_NAME,
    PUG_CATEGORY_NAME,
)
from pug.storage import (
    pug_data,
    pug_queue,
    pug_matches,
    save_pug_data,
    get_player,
    set_username,
    remove_username,
    username_to_discord,
    set_region,
    set_noadd,
    set_elo,
    reset_player,
    reset_profile,
    reset_elo_all,
    reset_queue_counter,
    get_noadd_info,
    get_user_mod_log,
)
from views.pug_queue import QueueView, build_queue_embed, build_leaderboard_embed, LeaderboardView


# /link
@bot.tree.command(
    name="link",
    description="Link a player's single Krunker account + region (replaces any existing one).",
    guild=guild_object(),
)
@is_pug_staff()
@app_commands.describe(
    member="The Discord member",
    username="Krunker username - CASE-SENSITIVE, type it exactly",
    region="Their region",
)
@app_commands.choices(region=[app_commands.Choice(name=r, value=r) for r in VALID_REGIONS])
async def link(interaction: discord.Interaction, member: discord.Member, username: str,
               region: app_commands.Choice[str]):
    username = username.strip()
    if not username:
        await interaction.response.send_message("Username can't be empty.", ephemeral=True)
        return
    owner = username_to_discord(username)
    if owner is not None and owner != member.id:
        await interaction.response.send_message(
            f"`{username}` is already linked to <@{owner}>. Unlink it there first (`/unlink`).",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    set_username(member.id, username)
    set_region(member.id, region.value)
    from pug.roles import apply_region_role
    await apply_region_role(interaction.guild, member, region.value)
    await interaction.response.send_message(
        f"Linked **{username}** ({region.value}) to {member.mention}.\n"
        f"-# Username is case-sensitive - make sure it matches Krunker exactly.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# /unlink
@bot.tree.command(
    name="unlink",
    description="Remove a Krunker username from a player.",
    guild=guild_object(),
)
@is_pug_staff()
@app_commands.describe(member="The Discord member", username="The Krunker username to remove")
async def unlink(interaction: discord.Interaction, member: discord.Member, username: str):
    names = remove_username(member.id, username.strip())
    remaining = ", ".join(f"`{n}`" for n in names) if names else "*(none)*"
    await interaction.response.send_message(
        f"Removed **{username}** from {member.mention}.\nRemaining: {remaining}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# /pug-setup
@bot.tree.command(
    name="pug-setup",
    description="Create the PUG category, #queue, Queue VC, and #results channels.",
    guild=guild_object(),
)
@is_pug_admin()
async def pug_setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Category
    category = discord.utils.get(guild.categories, name=PUG_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(PUG_CATEGORY_NAME)

    everyone = guild.default_role

    # #queue, locked: nobody can talk, bot can.
    queue_ch = discord.utils.get(guild.text_channels, name=QUEUE_CHANNEL_NAME)
    if not queue_ch:
        queue_ch = await guild.create_text_channel(
            QUEUE_CHANNEL_NAME,
            category=category,
            overwrites={
                everyone: discord.PermissionOverwrite(send_messages=False, view_channel=True),
                guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
            },
        )

    # #results
    results_ch = discord.utils.get(guild.text_channels, name=RESULTS_CHANNEL_NAME)
    if not results_ch:
        results_ch = await guild.create_text_channel(
            RESULTS_CHANNEL_NAME,
            category=category,
            overwrites={
                everyone: discord.PermissionOverwrite(send_messages=False, view_channel=True),
                guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
            },
        )

    # Persist config
    cfg = pug_data["config"]
    cfg["category_id"] = category.id
    cfg["queue_channel_id"] = queue_ch.id
    cfg["results_channel_id"] = results_ch.id

    # Auto-detect an existing #account-link channel (you create this one yourself)
    existing_link = discord.utils.get(guild.text_channels, name="account-link")
    if existing_link:
        cfg["account_link_channel_id"] = existing_link.id

    # Post (or repost) the persistent queue embed
    queue_msg = await queue_ch.send(embed=build_queue_embed(), view=QueueView())
    cfg["queue_message_id"] = queue_msg.id
    save_pug_data()

    await interaction.followup.send(
        f"PUG setup complete.\n"
        f"- Queue: {queue_ch.mention}\n"
        f"- Results: {results_ch.mention}\n"
        f"- Category: **{category.name}**\n"
        f"*(A match voice channel is created automatically when the queue pops.)*",
        ephemeral=True,
    )


# /pug-dashboard
@bot.tree.command(
    name="pug-set-account-link",
    description="Set the channel players are pointed to for linking their Krunker account.",
    guild=guild_object(),
)
@is_pug_admin()
@app_commands.describe(channel="The #account-link channel")
async def pug_set_account_link(interaction: discord.Interaction, channel: discord.TextChannel):
    from views.pug_link import build_account_link_embed, AccountLinkView
    pug_data["config"]["account_link_channel_id"] = channel.id
    msg = await channel.send(embed=build_account_link_embed(), view=AccountLinkView())
    pug_data["config"]["account_link_message_id"] = msg.id
    save_pug_data()
    await interaction.response.send_message(
        f"Account-link embed posted in {channel.mention}. Unlinked players are pointed there.",
        ephemeral=True,
    )


@bot.tree.command(
    name="pug-set-review-channel",
    description="Set the channel where account-link requests are sent for review.",
    guild=guild_object(),
)
@is_pug_admin()
@app_commands.describe(channel="The link-requests review channel (admin-only)")
async def pug_set_review_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    pug_data["config"]["review_channel_id"] = channel.id
    save_pug_data()
    await interaction.response.send_message(
        f"Account-link requests will be sent to {channel.mention} for review.", ephemeral=True
    )


@bot.tree.command(
    name="force-pop",
    description="Force the queue to pop now with whoever is queued (min 2).",
    guild=guild_object(),
)
@is_pug_staff()
async def force_pop(interaction: discord.Interaction):
    if len(pug_queue) < 2:
        await interaction.response.send_message(
            f"Need at least 2 players in the queue to force a pop (currently {len(pug_queue)}).",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"Force-popping the queue with {len(pug_queue)} player(s).", ephemeral=True
    )
    from pug.match import pop_queue
    await pop_queue(interaction.guild, interaction.client, force=True)


@bot.tree.command(
    name="pug-reset-counter",
    description="Reset the queue number counter (so test pugs don't keep incrementing).",
    guild=guild_object(),
)
@is_pug_staff()
@app_commands.describe(value="New counter value (default 0, so the next match is 0001)")
async def pug_reset_counter(interaction: discord.Interaction, value: int = 0):
    reset_queue_counter(value)
    await interaction.response.send_message(
        f"Queue counter reset. The next match will be **queue-{value + 1:04d}**.", ephemeral=True
    )


# /check
@bot.tree.command(
    name="check",
    description="Check a player's PUG profile: usernames, bans, and no-adds.",
    guild=guild_object(),
)
@is_pug_staff()
@app_commands.describe(user="The member to look up")
async def check(interaction: discord.Interaction, user: discord.Member):
    p = get_player(user.id)
    noadd = get_noadd_info(user.id)

    color = 0xDA3633 if noadd else 0x2B2D31
    embed = discord.Embed(title=f"PUG Profile | {user.display_name}", color=color)

    def status_line(info: dict, kind: str) -> str:
        reason = info.get("reason") or "unspecified reason"
        admin = info.get("admin") or "unknown"
        at = info.get("at")
        when = f"<t:{int(at)}:D>" if at else "an unknown date"
        line = f"**User is {kind} for {reason}** by {admin} on {when}"
        until = info.get("until")
        if until:
            line += f", expires <t:{int(until)}:R>"
        return line

    if noadd:
        embed.description = status_line(noadd, "no-added")

    usernames = p.get("usernames", [])
    embed.add_field(
        name="Linked Usernames",
        value=("\n".join(f"- {n}" for n in usernames) or "*none linked*"),
        inline=False,
    )
    embed.add_field(name="Region", value=(p.get("region") or "*not set*"), inline=False)
    embed.add_field(
        name="ELO",
        value=f"{p.get('elo', 0)} ({p.get('wins', 0)}W / {p.get('losses', 0)}L)",
        inline=False,
    )

    log = list(reversed(get_user_mod_log(user.id)))[:10]
    if log:
        lines = []
        for e in log:
            at = e.get("at")
            when = f"<t:{int(at)}:f>" if at else "?"
            reason = e.get("reason") or "-"
            admin = e.get("admin") or "?"
            lines.append(f"`{e['action']}` | {reason} | by **{admin}** | {when}")
        value = "\n".join(lines)
        embed.add_field(name="Recent Mod History", value=value[:1024], inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# /simulate
@bot.tree.command(
    name="simulate",
    description="Simulate a draft with fake players (you control both captains). Testing only.",
    guild=guild_object(),
)
@is_pug_staff()
@app_commands.describe(format="Match format to simulate")
@app_commands.choices(format=[app_commands.Choice(name="4v4", value="4v4")])
async def simulate(interaction: discord.Interaction, format: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    from pug.match import start_simulation
    match = await start_simulation(interaction.guild, interaction.client, interaction.user.id)
    await interaction.followup.send(
        f"Simulation started in <#{match['text_channel_id']}>, you control both captains.",
        ephemeral=True,
    )


@bot.tree.command(
    name="pug-cleanup",
    description="Delete leftover match channels orphaned by a restart/crash.",
    guild=guild_object(),
)
@is_pug_staff()
async def pug_cleanup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from pug.match import cleanup_orphaned_channels, reset_queue_on_start
    await reset_queue_on_start(interaction.client)
    n = await cleanup_orphaned_channels(interaction.guild)
    await interaction.followup.send(f"Cleaned up **{n}** orphaned match channel(s).", ephemeral=True)


# Moderation
def _until_from_hours(hours: float | None) -> float | None:
    if not hours or hours <= 0:
        return None
    return time.time() + hours * 3600


@bot.tree.command(name="noadd", description="No-add a player (block them from queueing).", guild=guild_object())
@is_pug_staff()
@app_commands.describe(user="Player to no-add", reason="Reason (optional)", hours="Duration in hours (blank = permanent)")
async def noadd(interaction: discord.Interaction, user: discord.Member, reason: str = "", hours: float = 0.0):
    set_noadd(user.id, True, reason=reason, until=_until_from_hours(hours), admin=interaction.user.display_name)
    if user.id in pug_queue:
        pug_queue.remove(user.id)
    dur = f" for {hours}h" if hours and hours > 0 else " permanently"
    await interaction.response.send_message(
        f"No-added {user.mention}{dur}. Reason: {reason or 'unspecified'}.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="unnoadd", description="Remove a player's no-add.", guild=guild_object())
@is_pug_staff()
@app_commands.describe(user="Player to un-no-add")
async def unnoadd(interaction: discord.Interaction, user: discord.Member):
    set_noadd(user.id, False, admin=interaction.user.display_name)
    await interaction.response.send_message(
        f"Removed no-add for {user.mention}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


# ELO management
@bot.tree.command(name="elo-set", description="Set a player's ELO to a specific value.", guild=guild_object())
@is_pug_staff()
@app_commands.describe(user="Player", elo="Desired ELO")
async def elo_set(interaction: discord.Interaction, user: discord.Member, elo: int):
    set_elo(user.id, elo)
    await interaction.response.send_message(
        f"Set {user.mention}'s ELO to **{elo}**.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="elo-reset",
    description="Reset ELO to 1000 for one player, or everyone if no user is given.",
    guild=guild_object(),
)
@is_pug_staff()
@app_commands.describe(user="Player to reset (leave blank to reset EVERYONE)")
async def elo_reset(interaction: discord.Interaction, user: discord.Member = None):
    if user:
        reset_player(user.id)
        await interaction.response.send_message(
            f"Reset {user.mention}'s ELO to **{ELO_START}** and cleared their W/L.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # Global reset is destructive, require an explicit confirmation click.
    count = len(pug_data["players"])
    await interaction.response.send_message(
        f"⚠️ This will reset **ELO + W/L for all {count} players** to {ELO_START}. "
        f"This cannot be undone.",
        view=ConfirmResetView(interaction.user.id),
        ephemeral=True,
    )


class ConfirmResetView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id

    @discord.ui.button(label="Reset EVERYONE", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return
        reset_elo_all()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Reset **everyone's** ELO to **{ELO_START}** and cleared all W/L.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled, no changes made.", view=self)


@bot.tree.command(
    name="pug-reset-profile",
    description="Wipe a player's linked usernames and region (ELO is untouched).",
    guild=guild_object(),
)
@is_pug_staff()
@app_commands.describe(user="Player whose linked accounts + region to clear")
async def pug_reset_profile(interaction: discord.Interaction, user: discord.Member):
    reset_profile(user.id)
    from pug.roles import apply_region_role
    await apply_region_role(interaction.guild, user, "")  # removes any NA/EU role
    await interaction.response.send_message(
        f"Reset {user.mention}'s profile - cleared linked usernames + region (and region role). "
        f"ELO is unchanged; use `/elo-reset` for that.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


# Match control
@bot.tree.command(name="match-fix", description="Manually set the winner of a live match.", guild=guild_object())
@is_pug_staff()
@app_commands.describe(match_id="Match number (e.g. 001)", winner="Winning team")
@app_commands.choices(winner=[
    app_commands.Choice(name="Team 1", value=1),
    app_commands.Choice(name="Team 2", value=2),
])
async def match_fix(interaction: discord.Interaction, match_id: str, winner: app_commands.Choice[int]):
    from pug.match import find_match_by_id, force_result
    match = find_match_by_id(match_id)
    if not match:
        await interaction.response.send_message(f"No live match found for `{match_id}`.", ephemeral=True)
        return
    if not match.get("captains") or not match.get("teams"):
        await interaction.response.send_message("That match hasn't formed teams yet.", ephemeral=True)
        return
    await force_result(match, winner.value, interaction.client, admin=interaction.user)
    await interaction.response.send_message(
        f"Recorded **{match['name'].upper()}** as a win for **Team {winner.value}**.", ephemeral=True,
    )


@bot.tree.command(
    name="cancel",
    description="Admin: cancel a match instantly. Captain: ask the other captain to agree.",
    guild=guild_object(),
)
@app_commands.describe(match_id="Match number (e.g. 001)")
async def cancel(interaction: discord.Interaction, match_id: str):
    from pug.match import find_match_by_id, cancel_match, request_cancel
    from pug.config import member_is_pug_staff
    match = find_match_by_id(match_id)
    if not match:
        await interaction.response.send_message(f"No live match found for `{match_id}`.", ephemeral=True)
        return

    # 1) Staff (admin/helper) cancel instantly, no agreement needed.
    if member_is_pug_staff(interaction.user):
        name = match["name"].upper()
        await cancel_match(match, interaction.client, headline=f"{interaction.user.mention} has cancelled this match.")
        await interaction.response.send_message(f"Cancelled **{name}**.", ephemeral=True)
        return

    # 2) A captain starts a cancellation request the other captain must agree to.
    if interaction.user.id in match.get("captains", []):
        if match.get("pending_cancel"):
            await interaction.response.send_message("A cancellation request is already pending.", ephemeral=True)
            return
        await request_cancel(match, interaction.user, interaction.client)
        await interaction.response.send_message("Cancellation request sent to the other captain.", ephemeral=True)
        return

    await interaction.response.send_message(
        "Only an admin or a captain in this match can cancel it.", ephemeral=True
    )


# Public info
@bot.tree.command(name="leaderboard", description="Show the PUG leaderboard.", guild=guild_object())
async def leaderboard(interaction: discord.Interaction):
    embed, total_pages = build_leaderboard_embed(0)
    view = LeaderboardView(0) if total_pages > 1 else None
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="live", description="Show all live PUG matches.", guild=guild_object())
async def live(interaction: discord.Interaction):
    active = [m for m in pug_matches.values() if m.get("phase") != "done"]
    if not active:
        await interaction.response.send_message("No live matches right now.")
        return

    def dn(match, pid):
        return match.get("names", {}).get(pid, str(pid))

    lines = []
    for m in sorted(active, key=lambda x: x.get("number", 0)):
        num = f"{m.get('number', 0):03d}"
        caps = m.get("captains") or []
        teams = m.get("teams") or {}
        if len(caps) == 2 and teams:
            t1 = ", ".join(dn(m, p) for p in teams.get(caps[0], []))
            t2 = ", ".join(dn(m, p) for p in teams.get(caps[1], []))
            lines.append(f"**Match {num}:** Team 1 - {t1} | Team 2 - {t2}")
        else:
            roster = ", ".join(dn(m, p) for p in m.get("players", []))
            lines.append(f"**Match {num}:** ({m.get('phase')}) {roster}")

    embed = discord.Embed(title="Live Matches", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed)
