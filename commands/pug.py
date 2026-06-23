import time

import discord
from discord import app_commands

from core.bot_instance import bot
from core.config import guild_object
from core.guild_views import GuildView
from pug.config import (
    is_pug_admin,
    is_pug_staff,
    ELO_START,
    VALID_REGIONS,
    QUEUE_CHANNEL_NAME,
    RESULTS_CHANNEL_NAME,
    PUG_CATEGORY_NAME,
    PUG_ADMIN_ROLE_ID,
    PUG_HELPER_ROLE_ID,
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
    primary_username,
    add_flag,
    remove_flag,
    reset_rating,
    reset_cached_stats,
    reset_cached_stats_all,
)
from views.pug_queue import (
    QueueView,
    build_queue_embed,
    build_normal_leaderboard,
    LeaderboardView,
    build_bigboard_embed,
    BigBoardView,
)


# /link
@bot.tree.command(
    name="link",
    description="Link a player's single Krunker account + region (replaces any existing one).",
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

    # #flags + #flags-log: private moderation channels (staff only).
    staff_overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    from core.guild_config import pug_admin_role_id, pug_helper_role_id
    for rid in (pug_admin_role_id(guild.id), pug_helper_role_id(guild.id)):
        role = guild.get_role(rid) if rid else None
        if role:
            staff_overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    flags_ch = discord.utils.get(guild.text_channels, name="flags")
    if not flags_ch:
        flags_ch = await guild.create_text_channel("flags", category=category, overwrites=staff_overwrites)
    flags_log_ch = discord.utils.get(guild.text_channels, name="flags-log")
    if not flags_log_ch:
        flags_log_ch = await guild.create_text_channel("flags-log", category=category, overwrites=staff_overwrites)

    # Persist config
    cfg = pug_data["config"]
    cfg["category_id"] = category.id
    cfg["queue_channel_id"] = queue_ch.id
    cfg["results_channel_id"] = results_ch.id
    cfg["flags_channel_id"] = flags_ch.id
    cfg["flags_log_channel_id"] = flags_log_ch.id

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
        f"- Flags: {flags_ch.mention}\n"
        f"- Flag log: {flags_log_ch.mention}\n"
        f"- Category: **{category.name}**\n"
        f"*(A match voice channel is created automatically when the queue pops.)*",
        ephemeral=True,
    )


# /pug-dashboard
@bot.tree.command(
    name="pug-set-account-link",
    description="Set the channel players are pointed to for linking their Krunker account.",
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


@bot.tree.command(name="noadd", description="No-add a player (block them from queueing).")
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


@bot.tree.command(name="unnoadd", description="Remove a player's no-add.")
@is_pug_staff()
@app_commands.describe(user="Player to un-no-add")
async def unnoadd(interaction: discord.Interaction, user: discord.Member):
    set_noadd(user.id, False, admin=interaction.user.display_name)
    await interaction.response.send_message(
        f"Removed no-add for {user.mention}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


# ELO management
@bot.tree.command(name="elo-set", description="Set a player's ELO to a specific value.")
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


@bot.tree.command(
    name="pug-reset-rating",
    description="Reset a player's average CKL rating (ELO, W/L, K/D, OBJ untouched).",
)
@is_pug_staff()
@app_commands.describe(user="Player whose average CKL rating to reset")
async def pug_reset_rating(interaction: discord.Interaction, user: discord.Member):
    reset_rating(user.id)
    await interaction.response.send_message(
        f"Reset {user.mention}'s average CKL rating. It will rebuild from their next game. "
        f"ELO, W/L, K/D, and OBJ are unchanged.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="pug-reset-stats",
    description="Fully reset cached PUG stats for one player, or everyone if no user is given.",
)
@is_pug_staff()
@app_commands.describe(user="Player to reset (leave blank to reset EVERYONE)")
async def pug_reset_stats(interaction: discord.Interaction, user: discord.Member = None):
    if user:
        reset_cached_stats(user.id)
        await interaction.response.send_message(
            f"Fully reset cached PUG stats for {user.mention}: ELO/W-L, K/D, OBJ/DMG, "
            f"CKL rating, trend history, peak ELO, and low-stat flags.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            from views.pug_queue import refresh_bigboard
            await refresh_bigboard(interaction.client)
        except Exception as e:
            print(f"[Pug] bigboard refresh failed after stat reset: {e}")
        return

    count = len(pug_data["players"])
    await interaction.response.send_message(
        f"Warning: this will fully reset cached PUG stats for **all {count} players**: "
        f"ELO/W-L, K/D, OBJ/DMG, CKL rating, trend history, peak ELO, and low-stat flags. "
        f"Linked accounts, regions, bans, no-adds, and mod logs are preserved. This cannot be undone.",
        view=ConfirmFullStatsResetView(interaction.user.id),
        ephemeral=True,
    )


class ConfirmFullStatsResetView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id

    @discord.ui.button(label="Reset ALL Stats", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return
        await interaction.response.defer()
        count = reset_cached_stats_all()
        try:
            from views.pug_queue import refresh_bigboard
            await refresh_bigboard(interaction.client)
        except Exception as e:
            print(f"[Pug] bigboard refresh failed after global stat reset: {e}")
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(
            content=f"Fully reset cached PUG stats for **{count} players**. Links, regions, and moderation records were preserved.",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled, no changes made.", view=self)


# Match control
@bot.tree.command(name="match-fix", description="Manually set the winner of a live match.")
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
@bot.tree.command(name="leaderboard", description="Show the PUG leaderboard (top 10, switch stats).")
async def leaderboard(interaction: discord.Interaction):
    embed, _, _ = build_normal_leaderboard("elo", 0)
    await interaction.response.send_message(embed=embed, view=LeaderboardView("elo", 0))


@bot.tree.command(
    name="pug-bigboard",
    description="Post the big standing leaderboard display in this channel (admin).",
)
@is_pug_admin()
async def pug_bigboard(interaction: discord.Interaction):
    cfg = pug_data["config"]
    cfg.setdefault("bigboard_stat", "elo")
    cfg["bigboard_page"] = 0
    msg = await interaction.channel.send(embed=build_bigboard_embed(), view=BigBoardView())
    cfg["bigboard_channel_id"] = interaction.channel.id
    cfg["bigboard_message_id"] = msg.id
    save_pug_data()
    await interaction.response.send_message(
        f"Big leaderboard posted in {interaction.channel.mention}. "
        f"It updates automatically after each match; anyone can switch the stat or page.",
        ephemeral=True,
    )


@bot.tree.command(name="live", description="Show all live PUG matches.")
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


@bot.tree.command(name="remove", description="Remove a player from the queue (staff only).")
@is_pug_staff()
@app_commands.describe(user="The member to remove from the queue")
async def remove(interaction: discord.Interaction, user: discord.Member):
    if user.id not in pug_queue:
        await interaction.response.send_message(
            f"{user.mention} isn't in the queue.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    pug_queue.remove(user.id)
    from views.pug_queue import refresh_queue_embed
    await refresh_queue_embed(interaction.client)
    await interaction.response.send_message(
        f"Removed {user.mention} from the queue.", ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ── /rank: the stat numbers + a per-stat trend graph you switch with buttons ─────
# Five stats -> one row of buttons; the numbers show on every tab, the graph swaps.
RANK_STAT_ORDER = ["elo", "kd", "rating", "obj", "wr"]
RANK_STAT_LABELS = {"elo": "ELO", "kd": "K/D", "rating": "CKL Rating",
                    "obj": "Avg OBJ", "wr": "Win Rate"}


def _rank_points(p: dict, stat_key: str) -> list:
    """Prepared [(x_label, value), ...] for a stat. ELO comes from the real per-game
    history (so it's backfilled immediately); the others come from the snapshot history
    (which builds up going forward) and aggregate to per-day past the limit."""
    from pug.graph import aggregate_series
    if stat_key == "elo":
        eh = list(p.get("elo_history") or [])
        return [(str(i + 1), v) for i, v in enumerate(eh)]
    series = [(s.get("ts", 0.0), s.get(stat_key, 0.0)) for s in p.get("stat_history", [])]
    return aggregate_series(series)


def build_rank_card(guild: discord.Guild, member: discord.Member, stat_key: str):
    """Build (embed, file): the player's stat numbers as fields on every tab, plus the
    graph for the selected stat as the image."""
    from pug.graph import draw_stat_graph
    from pug.backgrounds import get_background_bytes

    p = get_player(member.id)
    username = primary_username(member.id, member.display_name)
    region = (p.get("region") or "").upper()
    region_tag = f" [{region}]" if region else ""
    elo = p.get("elo", ELO_START)
    wins, losses = p.get("wins", 0), p.get("losses", 0)
    kills, deaths = p.get("kills", 0), p.get("deaths", 0)
    games = p.get("games", 0)
    kd_str = f"{kills / deaths:.2f}" if deaths else f"{float(kills):.0f}"
    avg_obj = round(p.get("obj", 0) / games) if games else 0
    rgames = p.get("rating_games", 0)
    avg_rating = round(p.get("rating_sum", 0.0) / rgames, 2) if rgames else 0.0
    peak = max(p.get("peak_elo", elo), elo, *(p.get("elo_history") or [elo]))

    embed = discord.Embed(title=f"Ranked Data for {username}{region_tag}", color=0x5865F2)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ELO (Peak)", value=f"{elo}  ({peak})", inline=True)
    embed.add_field(name="Record", value=f"{wins}W / {losses}L", inline=True)
    embed.add_field(name="K/D", value=f"{kd_str}  ({kills}/{deaths})", inline=True)
    embed.add_field(name="Avg OBJ", value=str(avg_obj), inline=True)
    embed.add_field(name="Avg CKL Rating", value=f"{avg_rating} / 10", inline=True)
    embed.add_field(name="Region", value=(region or "Not set"), inline=True)
    kd_flags, obj_flags = p.get("low_kd_flags", 0), p.get("low_obj_flags", 0)
    if kd_flags or obj_flags:
        embed.add_field(name="Flags", value=f"Low K/D: {kd_flags} | Low OBJ: {obj_flags}", inline=False)

    points = _rank_points(p, stat_key)
    bg = get_background_bytes(guild.id, member.id)
    graph = draw_stat_graph(points, stat_key, bg_bytes=bg)
    embed.set_image(url="attachment://rank.png")
    return embed, discord.File(graph, filename="rank.png")


class _RankStatButton(discord.ui.Button):
    def __init__(self, stat_key: str, active: bool):
        super().__init__(
            label=RANK_STAT_LABELS[stat_key],
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            row=0,
        )
        self.stat_key = stat_key

    async def callback(self, interaction: discord.Interaction):
        view: RankView = self.view
        member = interaction.guild.get_member(view.target_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(view.target_id)
            except discord.HTTPException:
                await interaction.response.send_message("Couldn't find that player anymore.", ephemeral=True)
                return
        embed, file = build_rank_card(interaction.guild, member, self.stat_key)
        await interaction.response.edit_message(
            embed=embed, attachments=[file], view=RankView(view.target_id, self.stat_key)
        )


class RankView(GuildView):
    def __init__(self, target_id: int, stat: str = "elo"):
        super().__init__(timeout=300)
        self.target_id = target_id
        self.stat = stat if stat in RANK_STAT_ORDER else "elo"
        # Five buttons -> a single row.
        for key in RANK_STAT_ORDER:
            self.add_item(_RankStatButton(key, active=(key == self.stat)))


@bot.tree.command(name="rank", description="Show a player's PUG rank card.")
@app_commands.describe(user="The player to look up (defaults to you)")
async def rank(interaction: discord.Interaction, user: discord.Member = None):
    await interaction.response.defer()
    target = user or interaction.user
    embed, file = build_rank_card(interaction.guild, target, "elo")
    await interaction.followup.send(embed=embed, file=file, view=RankView(target.id, "elo"))


@bot.tree.command(
    name="rank-customize",
    description="Set a custom background image for your rank card (linked players only).",
)
@app_commands.describe(pic="An image to use as your rank card background")
async def rank_customize(interaction: discord.Interaction, pic: discord.Attachment):
    if not get_player(interaction.user.id)["usernames"]:
        await interaction.response.send_message(
            "You need a linked Krunker account before customizing your rank card.", ephemeral=True
        )
        return
    if not (pic.content_type or "").startswith("image/"):
        await interaction.response.send_message("That file isn't an image.", ephemeral=True)
        return
    if pic.size and pic.size > 12 * 1024 * 1024:
        await interaction.response.send_message("That image is too large (max 12 MB).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    from pug.backgrounds import set_background
    try:
        data = await pic.read()
        set_background(interaction.guild.id, interaction.user.id, data)
    except Exception as e:
        await interaction.followup.send(f"Couldn't set that background: {e}", ephemeral=True)
        return
    embed, file = build_rank_card(interaction.guild, interaction.user, "elo")
    await interaction.followup.send("Rank background updated -- preview:", embed=embed, file=file, ephemeral=True)


@bot.tree.command(
    name="rank-customize-reset",
    description="Reset a player's rank card background (staff).",
)
@is_pug_admin()
@app_commands.describe(user="The player whose background to clear")
async def rank_customize_reset(interaction: discord.Interaction, user: discord.Member):
    from pug.backgrounds import clear_background
    cleared = clear_background(interaction.guild.id, user.id)
    msg = (f"Cleared {user.mention}'s rank background." if cleared
           else f"{user.mention} had no custom background set.")
    await interaction.response.send_message(
        msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@bot.tree.command(name="flag", description="Manually flag a player for low K/D or OBJ (staff only).")
@is_pug_staff()
@app_commands.describe(user="The player to flag", reason="What they're flagged for")
@app_commands.choices(reason=[
    app_commands.Choice(name="Low K/D", value="kd"),
    app_commands.Choice(name="Low OBJ", value="obj"),
])
async def flag(interaction: discord.Interaction, user: discord.Member, reason: app_commands.Choice[str]):
    count = add_flag(user.id, reason.value)
    from pug.match import _ordinal
    label = "low K/D" if reason.value == "kd" else "low OBJ"

    log_ch = interaction.client.get_channel(pug_data["config"].get("flags_log_channel_id"))
    if log_ch:
        embed = discord.Embed(
            description=(
                f"{user.mention} was manually flagged for **{label}** by {interaction.user.mention}.\n\n"
                f"This is their **{_ordinal(count)}** time being flagged for {label}."
            ),
            color=0xDA3633,
        )
        try:
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

    await interaction.response.send_message(
        f"Flagged {user.mention} for **{label}** ({_ordinal(count)} time).",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="unflag", description="Remove a flag from a player (staff only).")
@is_pug_staff()
@app_commands.describe(
    user="The player to unflag",
    reason="Which flag type to remove",
    clear_all="Remove ALL flags of this type instead of just one",
)
@app_commands.choices(reason=[
    app_commands.Choice(name="Low K/D", value="kd"),
    app_commands.Choice(name="Low OBJ", value="obj"),
])
async def unflag(
    interaction: discord.Interaction,
    user: discord.Member,
    reason: app_commands.Choice[str],
    clear_all: bool = False,
):
    label = "low K/D" if reason.value == "kd" else "low OBJ"
    new_count = remove_flag(user.id, reason.value, amount=10**9 if clear_all else 1)

    log_ch = interaction.client.get_channel(pug_data["config"].get("flags_log_channel_id"))
    if log_ch:
        verb = "cleared all" if clear_all else "removed a"
        embed = discord.Embed(
            description=(
                f"{interaction.user.mention} {verb} **{label}** flag from {user.mention}.\n\n"
                f"They now have **{new_count}** {label} flag(s)."
            ),
            color=0x3FB950,
        )
        try:
            await log_ch.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

    word = "Cleared all" if clear_all else "Removed a"
    await interaction.response.send_message(
        f"{word} **{label}** flag from {user.mention}. They now have **{new_count}**.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="pug-set-flags-channel", description="Set the channel where auto-flags are posted.")
@is_pug_admin()
@app_commands.describe(channel="The #flags channel")
async def pug_set_flags_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    pug_data["config"]["flags_channel_id"] = channel.id
    save_pug_data()
    await interaction.response.send_message(
        f"Underperformance flags will be posted in {channel.mention}.", ephemeral=True
    )


@bot.tree.command(name="pug-set-flags-log-channel", description="Set the channel where manual /flag logs are posted.")
@is_pug_admin()
@app_commands.describe(channel="The #flags-log channel")
async def pug_set_flags_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    pug_data["config"]["flags_log_channel_id"] = channel.id
    save_pug_data()
    await interaction.response.send_message(
        f"Manual flag logs will be posted in {channel.mention}.", ephemeral=True
    )


@bot.tree.command(
    name="pug-backfill-elo",
    description="Rebuild ELO-history graphs from old #results messages (admin only).",
)
@is_pug_admin()
@app_commands.describe(channel="Results channel to scan (defaults to the configured #results)")
async def pug_backfill_elo(interaction: discord.Interaction, channel: discord.TextChannel = None):
    import re
    await interaction.response.defer(ephemeral=True)

    results_ch = channel or interaction.client.get_channel(pug_data["config"].get("results_channel_id"))
    if not results_ch:
        await interaction.followup.send(
            "No results channel set. Run /pug-setup or pass a channel.", ephemeral=True
        )
        return

    # Result embeds list each player as: `<@id> | **elo** (delta)` in Winners/Losers fields.
    line_re = re.compile(r"<@!?(\d+)>\s*\|\s*\*\*(-?\d+)\*\*")
    histories: dict[int, list[int]] = {}
    messages_used = 0

    try:
        async for msg in results_ch.history(limit=None, oldest_first=True):
            used = False
            for embed in msg.embeds:
                for field in embed.fields:
                    if field.name not in ("Winners", "Losers"):
                        continue
                    for did_s, elo_s in line_re.findall(field.value or ""):
                        histories.setdefault(int(did_s), []).append(int(elo_s))
                        used = True
            if used:
                messages_used += 1
    except discord.Forbidden:
        await interaction.followup.send(
            f"I can't read history in {results_ch.mention}. Grant me Read Message History there.",
            ephemeral=True,
        )
        return

    if not histories:
        await interaction.followup.send(
            f"Scanned {results_ch.mention} but found no parseable result embeds.", ephemeral=True
        )
        return

    # Overwrite each found player's history (idempotent) and cap to the last 100 points.
    # Also recover peak ELO as the highest point ever seen.
    for did, hist in histories.items():
        player = get_player(did)
        player["elo_history"] = hist[-100:]
        player["peak_elo"] = max(player.get("peak_elo", 0), max(hist))
    save_pug_data()

    total_points = sum(len(h) for h in histories.values())
    await interaction.followup.send(
        f"Backfill complete. Parsed **{messages_used}** result messages and rebuilt "
        f"ELO history for **{len(histories)}** players ({total_points} data points).\n"
        f"*(Current ELO, wins, and losses were not changed. K/D and OBJ can't be "
        f"backfilled - they only existed in the scoreboard images.)*",
        ephemeral=True,
    )
