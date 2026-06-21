import asyncio
import io
import random
import re
import time
import urllib.parse

import discord

from core.config import MAPS, RAILWAY_BASE
from views.pickban import HostClientView, WEBHOOK_URL, MAP_IDS
from pug.config import (
    MATCH_SIZE,
    CHECKIN_SECONDS,
    REPING_INTERVAL,
    BRAND,
    BRAND_FULL,
)
from pug.storage import (
    pug_data,
    pug_queue,
    pug_matches,
    sim_players,
    get_elo,
    get_player,
    primary_username,
    username_to_discord,
    next_queue_number,
    record_match_stats,
    add_flag,
    in_active_match,
)


# helpers
def _add_spectator_overwrites(overwrites: dict, guild: discord.Guild, is_vc: bool) -> dict:
    """Grant staff + spectator/caster roles the SAME access players get on a pug
    channel: view/connect/speak/screenshare. The bot's own buttons gate by player
    identity, so these roles still can't draft, vote, or otherwise drive the match."""
    from pug.config import PUG_ADMIN_ROLE_ID, PUG_HELPER_ROLE_ID, PUG_SPECTATOR_ROLE_ID
    role_ids = {PUG_ADMIN_ROLE_ID, PUG_HELPER_ROLE_ID, PUG_SPECTATOR_ROLE_ID}
    for rid in role_ids:
        if not rid:
            continue
        role = guild.get_role(rid)
        if not role:
            continue
        if is_vc:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True
            )
        else:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    return overwrites


def _all_checked_in(match: dict) -> bool:
    size = match.get("size", MATCH_SIZE)
    return (
        len(match["players"]) == size
        and all(p in match["checked_in"] for p in match["players"])
    )


def _snake_order(num_picks: int) -> list[int]:
    """Snake draft captain order: 0,1, 1,0, 0,1, ... truncated to num_picks."""
    order = []
    r = 0
    while len(order) < num_picks:
        order.extend([0, 1] if r % 2 == 0 else [1, 0])
        r += 1
    return order[:num_picks]


# Match channels look like queue-0001, sim-0007, or "queue-0001 - Team 1"
_MATCH_CH_RE = re.compile(r"^(queue|sim)-\d+", re.IGNORECASE)


async def cleanup_orphaned_channels(guild: discord.Guild) -> int:
    """Delete leftover match channels (from a crash/restart) not owned by a live match."""
    cfg = pug_data["config"]
    category = guild.get_channel(cfg.get("category_id"))

    active_ids = set()
    for m in pug_matches.values():
        for k in ("text_channel_id", "checkin_vc_id", "team1_vc_id", "team2_vc_id"):
            if m.get(k):
                active_ids.add(m[k])

    candidates = list(category.channels) if category else [
        c for c in guild.channels if _MATCH_CH_RE.match(c.name or "")
    ]

    deleted = 0
    for ch in candidates:
        if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            continue
        if not _MATCH_CH_RE.match(ch.name or ""):
            continue
        if ch.id in active_ids:
            continue
        try:
            await ch.delete(reason="PUG orphaned-match cleanup")
            deleted += 1
        except discord.HTTPException:
            pass
    return deleted


async def reset_queue_on_start(bot):
    """After a restart the in-memory queue is empty; re-render the #queue embed so it
    doesn't show a stale roster."""
    pug_queue.clear()
    from views.pug_queue import refresh_queue_embed
    await refresh_queue_embed(bot)


def get_match_for_channel(channel_id: int) -> dict | None:
    return next((m for m in pug_matches.values() if m.get("text_channel_id") == channel_id), None)


def get_match_for_player(uid: int) -> dict | None:
    return next((m for m in pug_matches.values() if uid in m.get("players", [])), None)


def find_match_by_id(match_id: str) -> dict | None:
    """Resolve '001', '1', 'queue-0001', or 'sim-0001' to a live match."""
    s = str(match_id).strip().lstrip("#")
    if s in pug_matches:
        return pug_matches[s]
    try:
        n = int(s)
    except ValueError:
        return None
    return next((m for m in pug_matches.values() if m.get("number") == n), None)


async def cancel_match(match: dict, bot, headline: str = None, admin: discord.Member = None):
    """Tear down a live match without recording a result (10s delay + return players)."""
    if headline is None:
        who = admin.mention if admin else "An admin"
        headline = f"{who} has cancelled this match."
    embed = discord.Embed(
        description=(
            f"{headline}\n"
            f"All text and voice channels will be deleted {teardown_ts()}."
        ),
        color=0xFF4444,
    )
    await schedule_teardown(match, bot, embed)


async def request_cancel(match: dict, requester: discord.Member, bot):
    """A captain asks the other captain to agree to cancel the match."""
    caps = match.get("captains", [])
    match["pending_cancel"] = {"by": requester.id, "approved": {requester.id}}

    ch = bot.get_channel(match["text_channel_id"])
    others = " ".join(f"<@{c}>" for c in caps if c != requester.id)
    embed = discord.Embed(
        title="Cancellation Requested",
        description=(
            f"<@{requester.id}> wants to cancel this match.\n"
            f"The other captain must agree."
        ),
        color=0xFF4444,
    )
    from views.pug_cancel import CancelRequestView
    await ch.send(
        others,
        embed=embed,
        view=CancelRequestView(match["key"]),
        allowed_mentions=discord.AllowedMentions(users=True),
    )


async def abort_checkin(match: dict, bot, user: discord.Member):
    """Abort during the check-in stage: return checked-in players to the queue + origin VC."""
    task = match.get("checkin_task")
    if task:
        task.cancel()
    match["phase"] = "done"

    ch = bot.get_channel(match["text_channel_id"])
    if ch:
        try:
            await ch.send(
                embed=discord.Embed(
                    description=f"{user.mention} aborted check-in. Returning players to the queue.",
                    color=0xFF4444,
                ),
                delete_after=6,
            )
        except discord.HTTPException:
            pass

    # Checked-in players go back to the queue (appended, so anyone already waiting keeps priority)
    for pid in match.get("players", []):
        if pid in match.get("checked_in", set()) and pid not in pug_queue:
            pug_queue.append(pid)

    await _return_players_to_origin(match, bot)
    await _cleanup_channels(match, bot)
    pug_matches.pop(match["key"], None)

    from views.pug_queue import refresh_queue_embed
    await refresh_queue_embed(bot)
    await maybe_autopop(bot.get_guild(match["guild_id"]), bot)


async def force_result(match: dict, winner_team_num: int, bot, admin: discord.Member = None) -> dict:
    """Manually conclude a match: award ELO, post to #results, announce, clean up."""
    from pug.elo import apply_match
    cap1, cap2 = match["captains"]
    winner_cap = cap1 if winner_team_num == 1 else cap2
    loser_cap = cap2 if winner_team_num == 1 else cap1
    winner_ids = match["teams"][winner_cap]
    loser_ids = match["teams"][loser_cap]
    deltas = apply_match(winner_ids, loser_ids)
    win_name = match.get("names", {}).get(winner_cap, f"Team {winner_team_num}")

    results_ch = bot.get_channel(pug_data["config"].get("results_channel_id"))
    if results_ch:
        def line(pid):
            d = deltas.get(pid, 0)
            sign = "+" if d >= 0 else ""
            return f"<@{pid}> | **{get_elo(pid)}** ({sign}{d})"
        embed = discord.Embed(
            title=f"{match['name'].upper()} | Result (manual)",
            description=f"Winner: **Team {winner_team_num}** ({win_name})",
            color=0x3FB950,
        )
        embed.add_field(name="Winners", value="\n".join(line(p) for p in winner_ids), inline=True)
        embed.add_field(name="Losers", value="\n".join(line(p) for p in loser_ids), inline=True)
        await results_ch.send(embed=embed)

    who = admin.mention if admin else "An admin"
    teardown_embed = discord.Embed(
        description=(
            f"{who} has manually reported a win for **Team {winner_team_num}** ({win_name}).\n"
            f"All text and voice channels will be deleted {teardown_ts()}."
        ),
        color=0xFFA500,
    )
    await schedule_teardown(match, bot, teardown_embed)
    return deltas


# pop
async def maybe_autopop(guild: discord.Guild, bot):
    """Pop full matches (FIFO) after players were returned to the queue all at once
    (e.g. an aborted/cancelled game). Leftovers (<8) stay queued for the next game."""
    if not guild:
        return
    while len(pug_queue) >= MATCH_SIZE:
        await pop_queue(guild, bot)


async def pop_queue(guild: discord.Guild, bot, force: bool = False):
    """Start a check-in match. Normally requires a full MATCH_SIZE queue; a forced
    pop (admin dashboard) starts with whoever is queued (min 2, for testing)."""
    # Safety net: never pull anyone already in a live match into a new one, even if
    # they somehow slipped into the queue.
    for uid in [u for u in pug_queue if in_active_match(u)]:
        pug_queue.remove(uid)

    if force:
        size = len(pug_queue)
        if size < 2:
            return
    else:
        if len(pug_queue) < MATCH_SIZE:
            return
        size = MATCH_SIZE

    players = [pug_queue.pop(0) for _ in range(size)]
    number = next_queue_number()
    name = f"queue-{number:04d}"

    cfg = pug_data["config"]
    category = guild.get_channel(cfg.get("category_id"))

    names = {}
    for pid in players:
        member = guild.get_member(pid)
        names[pid] = primary_username(pid, member.display_name if member else str(pid))

    # Register the match in memory IMMEDIATELY - synchronously, before any `await` -
    # so the popped players are never in limbo (out of the queue but not yet in a
    # match). Otherwise a spam-clicked Join during channel creation below would find
    # them in neither and re-add them to the queue (TOCTOU race).
    match = {
        "key": name,
        "name": name,
        "number": number,
        "guild_id": guild.id,
        "text_channel_id": None,
        "checkin_vc_id": None,
        "team1_vc_id": None,
        "team2_vc_id": None,
        "players": players,
        "size": size,
        "names": names,
        "checked_in": set(),
        "origin_vc": {},   # pid -> VC they were in before the match (to return them)
        "votes": {},
        "phase": "checkin",
        "ping_message_id": None,
        "checkin_deadline": 0,
        "checkin_event": asyncio.Event(),
        # draft / pickban / host populated later
        "captains": [],
        "teams": {},
        "pool": [],
        "draft_order": [],
        "draft_pointer": 0,
        "map": None,
        "host_captain_id": None,
        "host_button_used": False,
        "host_message_id": None,
    }
    pug_matches[name] = match

    everyone = guild.default_role
    # Text channel: private to the match players.
    text_overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    # Voice channel: publicly visible (so people see games are live) but nobody can join.
    vc_overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=True, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True),
    }
    for pid in players:
        member = guild.get_member(pid)
        if member:
            text_overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            vc_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

    _add_spectator_overwrites(text_overwrites, guild, is_vc=False)
    _add_spectator_overwrites(vc_overwrites, guild, is_vc=True)

    text_ch = await guild.create_text_channel(name, category=category, overwrites=text_overwrites)
    checkin_vc = await guild.create_voice_channel(name, category=category, overwrites=vc_overwrites)
    match["text_channel_id"] = text_ch.id
    match["checkin_vc_id"] = checkin_vc.id

    # Drag any queued player who's already in a voice channel into the match VC
    # (this also counts as their check-in). Players not in voice get pinged as usual.
    for pid in players:
        member = guild.get_member(pid)
        if member and member.voice and member.voice.channel and member.voice.channel.id != checkin_vc.id:
            try:
                match["origin_vc"][pid] = member.voice.channel.id
                await member.move_to(checkin_vc)
                match["checked_in"].add(pid)
            except discord.HTTPException:
                pass

    # Anyone already sitting in the check-in VC counts immediately
    for m in checkin_vc.members:
        if m.id in players:
            match["checked_in"].add(m.id)

    match["checkin_task"] = asyncio.create_task(checkin_loop(match, bot))

    # Refresh the main queue embed (it just emptied by MATCH_SIZE)
    from views.pug_queue import refresh_queue_embed
    await refresh_queue_embed(bot)


# simulation
SIM_FAKE_BASE = 900_000_000


async def start_simulation(guild: discord.Guild, bot, controller_id: int):
    """Spin up a full 4v4 match with 7 fake players + the controller, auto-checked-in,
    skipping the check-in phase. The controller can act as either captain."""
    # Tear down any previous sim by this controller
    for key, m in list(pug_matches.items()):
        if m.get("sim_controller") == controller_id:
            await _cleanup_channels(m, bot)
            pug_matches.pop(key, None)
    sim_players.clear()

    fake_ids = []
    for i in range(1, 8):
        fid = SIM_FAKE_BASE + i
        sim_players[fid] = {
            "elo": random.randint(800, 1400),
            "wins": 0, "losses": 0,
            "usernames": [f"Bot{i}"],
            "fake": True,
        }
        fake_ids.append(fid)

    players = [controller_id] + fake_ids
    number = next_queue_number()
    name = f"sim-{number:04d}"

    cfg = pug_data["config"]
    category = guild.get_channel(cfg.get("category_id"))
    everyone = guild.default_role
    text_overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    vc_overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=True, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True),
    }
    controller = guild.get_member(controller_id)
    names = {controller_id: primary_username(controller_id, controller.display_name if controller else str(controller_id))}
    if controller:
        text_overwrites[controller] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        vc_overwrites[controller] = discord.PermissionOverwrite(view_channel=True, connect=True)
    for i, fid in enumerate(fake_ids, start=1):
        names[fid] = f"Bot{i}"

    _add_spectator_overwrites(text_overwrites, guild, is_vc=False)
    _add_spectator_overwrites(vc_overwrites, guild, is_vc=True)

    text_ch = await guild.create_text_channel(name, category=category, overwrites=text_overwrites)
    checkin_vc = await guild.create_voice_channel(name, category=category, overwrites=vc_overwrites)

    match = {
        "key": name,
        "name": name,
        "number": number,
        "guild_id": guild.id,
        "text_channel_id": text_ch.id,
        "checkin_vc_id": checkin_vc.id,
        "team1_vc_id": None,
        "team2_vc_id": None,
        "players": players,
        "size": 8,
        "names": names,
        "checked_in": set(players),   # everyone auto-checked-in
        "origin_vc": {},
        "votes": {},
        "phase": "checkin",
        "ping_message_id": None,
        "checkin_deadline": 0,
        "checkin_event": asyncio.Event(),
        "captains": [],
        "teams": {},
        "pool": [],
        "draft_order": [],
        "draft_pointer": 0,
        "map": None,
        "host_captain_id": None,
        "host_button_used": False,
        "host_message_id": None,
        "sim_controller": controller_id,
    }
    pug_matches[name] = match

    # Drag the controller into the match VC if they're in voice (mirrors a real pop;
    # the fake players can't be in voice). They'll get moved to their team VC at host.
    if controller and controller.voice and controller.voice.channel and controller.voice.channel.id != checkin_vc.id:
        try:
            match["origin_vc"][controller_id] = controller.voice.channel.id
            await controller.move_to(checkin_vc)
        except discord.HTTPException:
            pass

    await start_draft(match, bot)
    return match


# check-in
async def _send_ping(match: dict, bot):
    """Delete the previous ping message and post a fresh one that pings only the
    players who still need to check in, with a live countdown."""
    ch = bot.get_channel(match["text_channel_id"])
    if not ch:
        return

    # Delete the old ping message
    old_id = match.get("ping_message_id")
    if old_id:
        try:
            old = await ch.fetch_message(old_id)
            await old.delete()
        except discord.NotFound:
            pass

    unchecked = [p for p in match["players"] if p not in match["checked_in"]]
    checked = [p for p in match["players"] if p in match["checked_in"]]

    deadline = int(match["checkin_deadline"])
    pings = " ".join(f"<@{p}>" for p in unchecked)

    embed = discord.Embed(
        title=f"{match['name'].upper()} | Check In",
        description=(
            f"**Join the `{match['name']}` voice channel to check in.**\n\n"
            f"Time remaining: <t:{deadline}:R>\n"
            f"Un-checked players will be replaced from the queue."
        ),
        color=0xFFA500,
    )
    embed.add_field(
        name=f"Checked In ({len(checked)}/{match.get('size', MATCH_SIZE)})",
        value=("\n".join(f"Checked: <@{p}>" for p in checked) or "*nobody yet*"),
        inline=True,
    )
    embed.add_field(
        name=f"Waiting On ({len(unchecked)})",
        value=("\n".join(f"Waiting: <@{p}>" for p in unchecked) or "*everyone in*"),
        inline=True,
    )

    from views.pug_checkin import CheckinView
    msg = await ch.send(
        pings or "All players checked in.",
        embed=embed,
        view=CheckinView(match["key"]),
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    match["ping_message_id"] = msg.id


async def checkin_loop(match: dict, bot):
    """Run check-in with backfill until the match is full of checked-in players or
    it dissolves for lack of replacements."""
    try:
        while True:
            if _all_checked_in(match):
                await start_draft(match, bot)
                return

            match["checkin_deadline"] = time.time() + CHECKIN_SECONDS
            await _send_ping(match, bot)

            # Tick until the deadline, waking early if someone checks in.
            while time.time() < match["checkin_deadline"]:
                remaining = match["checkin_deadline"] - time.time()
                try:
                    await asyncio.wait_for(
                        match["checkin_event"].wait(),
                        timeout=min(REPING_INTERVAL, max(0.1, remaining)),
                    )
                except asyncio.TimeoutError:
                    pass
                match["checkin_event"].clear()

                if _all_checked_in(match):
                    await start_draft(match, bot)
                    return
                await _send_ping(match, bot)

            # Deadline reached, drop AFK players
            not_in = [p for p in match["players"] if p not in match["checked_in"]]
            for p in not_in:
                match["players"].remove(p)

            # Backfill from the queue
            size = match.get("size", MATCH_SIZE)
            guild = bot.get_guild(match["guild_id"])
            while len(match["players"]) < size and pug_queue:
                npid = pug_queue.pop(0)
                match["players"].append(npid)
                nm = guild.get_member(npid) if guild else None
                match["names"][npid] = primary_username(npid, nm.display_name if nm else str(npid))
                # Grant the backfilled player access so they can see the channel and
                # join the check-in VC (the original overwrites only covered the first 8).
                if nm:
                    for cid, is_vc in (
                        (match.get("text_channel_id"), False),
                        (match.get("checkin_vc_id"), True),
                    ):
                        ch = guild.get_channel(cid) if cid else None
                        if not ch:
                            continue
                        try:
                            if is_vc:
                                await ch.set_permissions(nm, view_channel=True, connect=True)
                            else:
                                await ch.set_permissions(nm, view_channel=True, send_messages=True)
                        except discord.HTTPException:
                            pass

            from views.pug_queue import refresh_queue_embed
            await refresh_queue_embed(bot)

            if len(match["players"]) < size:
                await dissolve_match(match, bot, reason="Not enough players checked in.")
                return
            # else: loop again with a fresh deadline for the new/unchecked players
    except asyncio.CancelledError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Pug] check-in loop error: {e}")


def mark_checked_in(match: dict, uid: int):
    if uid in match["players"]:
        match["checked_in"].add(uid)
        match["checkin_event"].set()


async def dissolve_match(match: dict, bot, reason: str = ""):
    """Return checked-in players to the queue and tear down the channels."""
    ch = bot.get_channel(match["text_channel_id"])
    returning = [p for p in match["players"] if p in match["checked_in"]]
    for p in returning:
        if p not in pug_queue:
            pug_queue.append(p)

    if ch and returning:
        try:
            await ch.send(
                f"Match cancelled. {reason}\n"
                f"Returned to queue: {' '.join(f'<@{p}>' for p in returning)}",
                delete_after=10,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass

    await _return_players_to_origin(match, bot)
    await _cleanup_channels(match, bot)
    pug_matches.pop(match["key"], None)

    from views.pug_queue import refresh_queue_embed
    await refresh_queue_embed(bot)
    await maybe_autopop(bot.get_guild(match["guild_id"]), bot)


# draft
async def start_draft(match: dict, bot):
    if match["phase"] != "checkin":
        return
    match["phase"] = "draft"

    # Remove the now-stale check-in message (otherwise the last checked-in player still
    # shows as "waiting" while the draft starts).
    ch0 = bot.get_channel(match["text_channel_id"])
    if ch0 and match.get("ping_message_id"):
        try:
            old = await ch0.fetch_message(match["ping_message_id"])
            await old.delete()
        except discord.NotFound:
            pass
    match["ping_message_id"] = None

    # Captains: players with the captain-priority role rank first, then by ELO.
    # Random shuffle first so ties within the same (role, ELO) break randomly.
    from pug.config import PUG_CAPTAIN_ROLE_ID
    guild_for_roles = bot.get_guild(match["guild_id"])

    def _has_priority(pid):
        if not PUG_CAPTAIN_ROLE_ID or not guild_for_roles:
            return False
        m = guild_for_roles.get_member(pid)
        return bool(m and any(r.id == PUG_CAPTAIN_ROLE_ID for r in m.roles))

    ordered = match["players"][:]
    random.shuffle(ordered)
    ordered.sort(key=lambda p: (_has_priority(p), get_elo(p)), reverse=True)
    captains = ordered[:2]
    match["captains"] = captains
    match["teams"] = {captains[0]: [captains[0]], captains[1]: [captains[1]]}
    match["pool"] = [p for p in ordered if p not in captains]
    match["draft_order"] = _snake_order(len(match["pool"]))
    match["draft_pointer"] = 0

    # No players left to draft (tiny test pop) -> straight to map vote
    if not match["pool"]:
        await start_map_vote(match, bot)
        return

    ch = bot.get_channel(match["text_channel_id"])
    from views.pug_draft import build_draft_embed, PugDraftView
    view = PugDraftView(match["key"])
    view.rebuild()
    start_draft_timer(match, bot)
    # Everything lives in the embed; we only ping the two captains so they get notified.
    msg = await ch.send(
        f"<@{captains[0]}> <@{captains[1]}>",
        embed=build_draft_embed(match),
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    match["draft_message_id"] = msg.id


# draft auto-pick timer
DRAFT_PICK_SECONDS = 30


def start_draft_timer(match: dict, bot):
    old = match.get("draft_task")
    if old:
        old.cancel()
    match["draft_deadline"] = time.time() + DRAFT_PICK_SECONDS
    match["draft_task"] = asyncio.create_task(_draft_timer(match, bot))


async def _draft_timer(match: dict, bot):
    try:
        await asyncio.sleep(DRAFT_PICK_SECONDS)
    except asyncio.CancelledError:
        return
    if match.get("phase") != "draft":
        return
    pool = sorted(match.get("pool", []), key=lambda p: get_elo(p), reverse=True)
    if not pool:
        return
    # Auto-pick the highest-ELO available player for the captain who timed out.
    from views.pug_draft import apply_pick, render_after_pick
    complete = apply_pick(match, pool[0])
    await render_after_pick(match, bot, complete)


# map vote (15s)
MAP_VOTE_SECONDS = 15


async def start_map_vote(match: dict, bot):
    # Cancel the draft timer - but NOT if we're being called from inside it (auto-pick
    # path), since cancelling the current task would abort this coroutine mid-setup.
    t = match.get("draft_task")
    if t and t is not asyncio.current_task():
        t.cancel()
    match["phase"] = "vote"
    match["votes"] = {}
    match["vote_attempts"] = 0
    await _send_vote(match, bot, ping=False)


async def _send_vote(match: dict, bot, ping: bool):
    match["vote_deadline"] = time.time() + MAP_VOTE_SECONDS
    ch = bot.get_channel(match["text_channel_id"])
    if not ch:
        return
    # Remove the previous vote message (re-prompt case)
    old = match.get("vote_message_id")
    if old:
        try:
            m = await ch.fetch_message(old)
            await m.delete()
        except discord.NotFound:
            pass
    from views.pug_vote import build_vote_embed, MapVoteView
    content = None
    if ping:
        content = " ".join(f"<@{p}>" for p in match["players"]) + "\n**Nobody voted - vote for a map!**"
    msg = await ch.send(
        content=content,
        embed=build_vote_embed(match),
        view=MapVoteView(match["key"]),
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    match["vote_message_id"] = msg.id
    match["vote_task"] = asyncio.create_task(_vote_timer(match, bot))


async def _vote_timer(match: dict, bot):
    try:
        await asyncio.sleep(MAP_VOTE_SECONDS)
        await finalize_vote(match, bot)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[Pug] vote timer error: {e}")


async def finalize_vote(match: dict, bot):
    if match.get("phase") != "vote":
        return

    votes = match.get("votes", {})
    if not votes:
        # Nobody voted - re-ping and re-prompt a couple times before falling back to random.
        if match.get("vote_attempts", 0) < 2:
            match["vote_attempts"] = match.get("vote_attempts", 0) + 1
            await _send_vote(match, bot, ping=True)
            return
        chosen = random.choice(MAPS)
    else:
        tally: dict[str, int] = {}
        for m in votes.values():
            tally[m] = tally.get(m, 0) + 1
        top = max(tally.values())
        chosen = random.choice([m for m, c in tally.items() if c == top])

    match["map"] = chosen

    # Lock the vote message (remove buttons, show the result)
    ch = bot.get_channel(match["text_channel_id"])
    if ch and match.get("vote_message_id"):
        from views.pug_vote import build_vote_embed
        try:
            msg = await ch.fetch_message(match["vote_message_id"])
            await msg.edit(embed=build_vote_embed(match, final=True), view=None)
        except discord.NotFound:
            pass

    await post_host(match, bot)


# host
def _team_usernames(match: dict, captain_id: int, guild: discord.Guild) -> list[str]:
    names = []
    for pid in match["teams"][captain_id]:
        member = guild.get_member(pid)
        fallback = member.display_name if member else str(pid)
        names.append(primary_username(pid, fallback))
    return names


def build_host_url(match: dict, client: str, guild: discord.Guild) -> str:
    cap1, cap2 = match["captains"]
    n1 = len(match["teams"][cap1])
    n2 = len(match["teams"][cap2])
    params = {
        "action": "host-comp",
        "mapId": MAP_IDS.get(match["map"], match["map"]),
        "team1Name": primary_username(cap1, f"Team {cap1}"),
        "team2Name": primary_username(cap2, f"Team {cap2}"),
        "teamSize": f"{max(n1, n2)}v{max(n1, n2)}",
        "team1Players": ", ".join(_team_usernames(match, cap1, guild)),
        "team2Players": ", ".join(_team_usernames(match, cap2, guild)),
        "region": match.get("region_code", "DAL").lower(),
        "webhook": WEBHOOK_URL,
    }
    query = urllib.parse.urlencode(params)
    return f"{RAILWAY_BASE}/launch?client={client}&{query}"


def compute_host_region(match: dict) -> tuple[str, str]:
    """Pick the host server from lobby composition. Default Dallas; EU-heavy lobbies
    shift to New York, very EU-heavy to Frankfurt, so EU doesn't always get screwed."""
    na = sum(1 for p in match.get("players", []) if get_player(p).get("region", "").upper() == "NA")
    eu = sum(1 for p in match.get("players", []) if get_player(p).get("region", "").upper() == "EU")
    if eu >= 6 and na <= 2:
        return "FRA", "Frankfurt"
    if eu >= 3 and na <= 5:
        return "NY", "New York"
    return "DAL", "Dallas"


async def post_host(match: dict, bot):
    match["phase"] = "host"
    guild = bot.get_guild(match["guild_id"])
    ch = bot.get_channel(match["text_channel_id"])
    cfg = pug_data["config"]
    category = guild.get_channel(cfg.get("category_id"))

    cap1, cap2 = match["captains"]
    match["host_captain_id"] = cap1  # higher ELO captain hosts
    match["region_code"], match["region_name"] = compute_host_region(match)

    # Create team voice channels and move players in.
    # Publicly visible (spectators can see who's playing) but only team members can join.
    everyone = guild.default_role
    base_overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=True, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True),
    }
    for cap, slot in ((cap1, 1), (cap2, 2)):
        ow = dict(base_overwrites)
        for pid in match["teams"][cap]:
            member = guild.get_member(pid)
            if member:
                ow[member] = discord.PermissionOverwrite(view_channel=True, connect=True)
        _add_spectator_overwrites(ow, guild, is_vc=True)
        vc = await guild.create_voice_channel(
            f"{match['name']} - Team {slot}", category=category, overwrites=ow
        )
        match[f"team{slot}_vc_id"] = vc.id
        for pid in match["teams"][cap]:
            member = guild.get_member(pid)
            if member and member.voice and member.voice.channel:
                try:
                    await member.move_to(vc)
                except discord.HTTPException:
                    pass

    from views.pug_pickban import PugHostView
    view = PugHostView(match["key"])
    msg = await ch.send(
        f"<@{cap1}> <@{cap2}>",
        embed=build_host_embed(match),
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    match["host_message_id"] = msg.id


def build_host_embed(match: dict) -> discord.Embed:
    cap1, cap2 = match["captains"]
    names = match.get("names", {})

    def dn(pid):
        return names.get(pid, str(pid))

    t1 = ", ".join(dn(p) for p in match["teams"][cap1])
    t2 = ", ".join(dn(p) for p in match["teams"][cap2])
    region_name = match.get("region_name", "Dallas")
    embed = discord.Embed(
        title="Click to Host",
        description=(
            f"**Map:** {match['map']}\n"
            f"**Region:** {region_name}\n\n"
            f"**Team 1**: {t1}\n"
            f"**Team 2**: {t2}\n\n"
            f"**Anyone** can host: click below, create the lobby, and paste the Krunker link here. "
            f"The first valid link is used."
        ),
        color=0xFFA500,
    )
    embed.set_footer(text=f"Make sure your region is set to {region_name.upper()}.")
    return embed


# subs
async def replace_player(match: dict, out_id: int, in_id: int, bot):
    """Swap out_id for in_id everywhere in the match, then refresh the live embed."""
    guild = bot.get_guild(match["guild_id"])

    if out_id in match["players"]:
        match["players"][match["players"].index(out_id)] = in_id
    match["checked_in"].discard(out_id)
    match["checked_in"].add(in_id)

    # Record the incoming player's display name for buttons
    in_m0 = guild.get_member(in_id) if guild else None
    match.setdefault("names", {})[in_id] = primary_username(in_id, in_m0.display_name if in_m0 else str(in_id))

    # Drop any vote the outgoing player had
    match.get("votes", {}).pop(out_id, None)

    if out_id in match.get("pool", []):
        match["pool"][match["pool"].index(out_id)] = in_id

    if out_id in match.get("captains", []):
        # Incoming player inherits captaincy + team + host role
        match["captains"][match["captains"].index(out_id)] = in_id
        if out_id in match.get("teams", {}):
            old = match["teams"].pop(out_id)
            match["teams"][in_id] = [in_id] + [p for p in old if p != out_id]
        if match.get("host_captain_id") == out_id:
            match["host_captain_id"] = in_id
    else:
        for members in match.get("teams", {}).values():
            if out_id in members:
                members[members.index(out_id)] = in_id
                break

    await _swap_channel_perms(match, out_id, in_id, guild)
    await refresh_phase_message(match, bot)


async def _swap_channel_perms(match: dict, out_id: int, in_id: int, guild):
    out_m = guild.get_member(out_id)
    in_m = guild.get_member(in_id)
    for cid in (match.get("text_channel_id"), match.get("checkin_vc_id"),
                match.get("team1_vc_id"), match.get("team2_vc_id")):
        if not cid:
            continue
        ch = guild.get_channel(cid)
        if not ch:
            continue
        try:
            if out_m:
                await ch.set_permissions(out_m, overwrite=None)
            if in_m:
                if isinstance(ch, discord.VoiceChannel):
                    await ch.set_permissions(in_m, view_channel=True, connect=True)
                else:
                    await ch.set_permissions(in_m, view_channel=True, send_messages=True)
        except discord.HTTPException:
            pass

    # If we're already at the host stage, drag the incoming player into their team VC
    if match.get("phase") == "host" and in_m and in_m.voice and in_m.voice.channel:
        cap1, cap2 = match["captains"]
        target = None
        if in_id in match["teams"].get(cap1, []):
            target = match.get("team1_vc_id")
        elif in_id in match["teams"].get(cap2, []):
            target = match.get("team2_vc_id")
        if target:
            vc = guild.get_channel(target)
            if vc:
                try:
                    await in_m.move_to(vc)
                except discord.HTTPException:
                    pass


async def refresh_phase_message(match: dict, bot):
    """Re-render the embed for whatever phase the match is in (after a sub)."""
    ch = bot.get_channel(match["text_channel_id"])
    if not ch:
        return
    phase = match.get("phase")
    try:
        if phase == "draft" and match.get("draft_message_id"):
            from views.pug_draft import build_draft_embed, PugDraftView
            v = PugDraftView(match["key"]); v.rebuild()
            msg = await ch.fetch_message(match["draft_message_id"])
            await msg.edit(embed=build_draft_embed(match), view=v)
        elif phase == "vote" and match.get("vote_message_id"):
            from views.pug_vote import build_vote_embed, MapVoteView
            msg = await ch.fetch_message(match["vote_message_id"])
            await msg.edit(embed=build_vote_embed(match), view=MapVoteView(match["key"]))
        elif phase == "host" and match.get("host_message_id"):
            from views.pug_pickban import PugHostView
            msg = await ch.fetch_message(match["host_message_id"])
            await msg.edit(embed=build_host_embed(match), view=PugHostView(match["key"]))
    except discord.NotFound:
        pass


# cleanup
async def _cleanup_channels(match: dict, bot):
    for cid_key in ("text_channel_id", "checkin_vc_id", "team1_vc_id", "team2_vc_id"):
        cid = match.get(cid_key)
        if not cid:
            continue
        ch = bot.get_channel(cid)
        if ch:
            try:
                await ch.delete()
            except discord.HTTPException:
                pass


TEARDOWN_DELAY = 10


def teardown_ts(delay: int = TEARDOWN_DELAY) -> str:
    return f"<t:{int(time.time()) + delay}:R>"


async def _return_players_to_origin(match: dict, bot):
    """Move any player still sitting in a match VC back to the VC they came from."""
    guild = bot.get_guild(match["guild_id"])
    if not guild:
        return
    match_vc_ids = {match.get("checkin_vc_id"), match.get("team1_vc_id"), match.get("team2_vc_id")}
    match_vc_ids.discard(None)
    origins = match.get("origin_vc", {})
    for pid in match.get("players", []):
        member = guild.get_member(pid)
        if not member or not member.voice or not member.voice.channel:
            continue
        if member.voice.channel.id not in match_vc_ids:
            continue
        dest = guild.get_channel(origins.get(pid)) if origins.get(pid) else None
        if dest:
            try:
                await member.move_to(dest)
            except discord.HTTPException:
                pass


async def schedule_teardown(match: dict, bot, embed: discord.Embed | None, delay: int = TEARDOWN_DELAY):
    """Post a teardown notice, then after `delay`s return players to their origin VC
    and delete the match channels."""
    match["phase"] = "done"
    for task_key in ("checkin_task", "vote_task", "draft_task"):
        task = match.get(task_key)
        if task:
            task.cancel()

    ch = bot.get_channel(match["text_channel_id"])
    if ch and embed:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass

    asyncio.create_task(_teardown_after(match, bot, delay))


async def _teardown_after(match: dict, bot, delay: int):
    try:
        await asyncio.sleep(delay)
        await _return_players_to_origin(match, bot)
        await _cleanup_channels(match, bot)
    finally:
        pug_matches.pop(match["key"], None)
        if match.get("sim_controller"):
            sim_players.clear()


# webhook result handling
def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def _detect_and_post_flags(bot, players, winner_krunker, loser_krunker, winner_team_num, map_name):
    """Flag linked players who went near-double-negative on K/D or contributed under
    FLAG_OBJ_TEAM_SHARE of their team's OBJ. Posts an embed + magnified scoreboard to
    the #flags channel and increments the player's flag counter."""
    from pug.config import FLAG_KD_RATIO, FLAG_KD_MIN_DEATHS, FLAG_OBJ_TEAM_SHARE
    flags_ch = bot.get_channel(pug_data["config"].get("flags_channel_id"))
    if not flags_ch:
        return

    # Team OBJ totals (by payload team number) for the share calculation.
    team_obj_total: dict = {}
    for p in players:
        team_obj_total[p.get("team")] = team_obj_total.get(p.get("team"), 0) + int(p.get("objective_score", 0))

    from scoreboard import draw_flag_scoreboard, RED, WHITE
    winner_players = sorted([p for p in players if p["team"] == winner_team_num], key=lambda p: p.get("score", 0), reverse=True)
    loser_players = sorted([p for p in players if p["team"] != winner_team_num], key=lambda p: p.get("score", 0), reverse=True)

    def _board(name, column):
        return draw_flag_scoreboard(
            tournament_name=BRAND_FULL, map_name=map_name,
            team1_name=winner_krunker["name"], team1_score=winner_krunker.get("score", 0),
            team1_players=winner_players, team1_color=RED,
            team2_name=loser_krunker["name"], team2_score=loser_krunker.get("score", 0),
            team2_players=loser_players, team2_color=WHITE,
            highlight_name=name, column=column,
        )

    for p in players:
        name = p.get("name", "")
        did = username_to_discord(name)
        if did is None:
            continue
        kills = int(p.get("kills", 0))
        deaths = int(p.get("deaths", 0))
        obj = int(p.get("objective_score", 0))

        # Low K/D: near double-negative or worse, with a deaths floor so small samples
        # (e.g. 2-5) don't trip it. kills==0 is caught by the floor alone.
        if deaths >= FLAG_KD_MIN_DEATHS and deaths >= FLAG_KD_RATIO * kills:
            count = add_flag(did, "kd")
            embed = discord.Embed(
                description=(
                    f"<@{did}> has been flagged for going **{kills}-{deaths}**.\n\n"
                    f"This is their **{_ordinal(count)}** time being flagged for a low K/D."
                ),
                color=0xDA3633,
            )
            embed.set_image(url="attachment://flag.png")
            try:
                await flags_ch.send(
                    embed=embed,
                    file=discord.File(_board(name, "kd"), filename="flag.png"),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException as e:
                print(f"[Pug] failed to post kd flag: {e}")

        # Low OBJ: under the team share threshold.
        team_total = team_obj_total.get(p.get("team"), 0)
        if team_total > 0 and obj < FLAG_OBJ_TEAM_SHARE * team_total:
            share = round(100 * obj / team_total)
            count = add_flag(did, "obj")
            embed = discord.Embed(
                description=(
                    f"<@{did}> has been flagged for getting **{obj} OBJ** ({share}% of their team's OBJ).\n\n"
                    f"This is their **{_ordinal(count)}** time being flagged for low OBJ."
                ),
                color=0xDA3633,
            )
            embed.set_image(url="attachment://flag.png")
            try:
                await flags_ch.send(
                    embed=embed,
                    file=discord.File(_board(name, "obj"), filename="flag.png"),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException as e:
                print(f"[Pug] failed to post obj flag: {e}")


async def try_handle_pug_match_end(payload: dict) -> bool:
    """Called first by the Krunker webhook. Returns True if this was a PUG match."""
    if payload.get("type") != "match_end":
        return False

    teams = payload.get("teams", [])
    players = payload.get("players", [])
    winner_team_num = payload.get("winner")
    map_name = payload.get("map", "Unknown")
    if len(teams) < 2 or winner_team_num is None:
        return False

    active = [m for m in pug_matches.values() if m.get("phase") == "host" and m.get("captains")]
    payload_team_names = {
        str(teams[0].get("name", "")).strip().lower(),
        str(teams[1].get("name", "")).strip().lower(),
    }
    print(f"[Pug] match_end: teams={[t.get('name') for t in teams]} winner={winner_team_num} "
          f"players={[p.get('name') for p in players]} | active matches={len(active)}")

    # Map payload player krunker names -> discord ids, grouped by team number.
    team_player_ids: dict = {}
    all_payload_ids = set()
    for p in players:
        did = username_to_discord(p.get("name", ""))
        if did is not None:
            team_player_ids.setdefault(p.get("team"), set()).add(did)
            all_payload_ids.add(did)

    # 1) Match by player identity overlap (robust - independent of team-name strings).
    match = None
    if all_payload_ids:
        best = 0
        for m in active:
            overlap = len(set(m.get("players", [])) & all_payload_ids)
            if overlap > best:
                best, match = overlap, m

    # 2) Fallback: captain primary usernames == reported team names.
    if not match:
        for m in active:
            c1, c2 = m["captains"]
            cap_names = {
                primary_username(c1, "").strip().lower(),
                primary_username(c2, "").strip().lower(),
            }
            if cap_names == payload_team_names and "" not in cap_names:
                match = m
                break

    if not match:
        print(f"[Pug] match_end: no PUG match found. payload team names={payload_team_names}; "
              f"active captains={[[primary_username(c, '') for c in m['captains']] for m in active]}. "
              f"(Are all players linked via /link? Does a team name match a captain's primary username?)")
        return False

    from core.bot_instance import bot
    from pug.elo import apply_match

    cap1, cap2 = match["captains"]

    winner_krunker = next((t for t in teams if t.get("team") == winner_team_num), None)
    loser_krunker = next((t for t in teams if t.get("team") != winner_team_num), None)
    if not winner_krunker or not loser_krunker:
        return True  # it's ours but malformed; swallow it

    # Decide which match team won - prefer player membership, fall back to team-name.
    team1_ids = set(match["teams"][cap1])
    team2_ids = set(match["teams"][cap2])
    winner_payload_ids = team_player_ids.get(winner_team_num, set())
    if winner_payload_ids & team1_ids:
        winner_cap, loser_cap = cap1, cap2
    elif winner_payload_ids & team2_ids:
        winner_cap, loser_cap = cap2, cap1
    else:
        cap1_name = primary_username(cap1, "").strip().lower()
        winner_is_cap1 = str(winner_krunker.get("name", "")).strip().lower() == cap1_name
        winner_cap = cap1 if winner_is_cap1 else cap2
        loser_cap = cap2 if winner_is_cap1 else cap1

    winner_ids = match["teams"][winner_cap]
    loser_ids = match["teams"][loser_cap]

    # Record per-game kills/deaths/obj for every linked player BEFORE apply_match, so
    # the stats are persisted in the same save as the ELO update.
    match_rounds = int(winner_krunker.get("score", 0)) + int(loser_krunker.get("score", 0))
    for p in players:
        did = username_to_discord(p.get("name", ""))
        if did is not None:
            record_match_stats(did, p.get("kills", 0), p.get("deaths", 0),
                                p.get("objective_score", 0), p.get("damage_done", 0), match_rounds)

    deltas = apply_match(winner_ids, loser_ids)

    # Build scoreboard image
    image_file = None
    try:
        from scoreboard import draw_scoreboard, RED, WHITE
        winner_players = sorted(
            [p for p in players if p["team"] == winner_team_num],
            key=lambda p: p.get("score", 0), reverse=True,
        )
        loser_players = sorted(
            [p for p in players if p["team"] != winner_team_num],
            key=lambda p: p.get("score", 0), reverse=True,
        )
        img_buf = draw_scoreboard(
            tournament_name=BRAND_FULL,
            map_name=map_name,
            team1_name=winner_krunker["name"],
            team1_score=winner_krunker.get("score", 0),
            team1_players=winner_players,
            team1_color=RED,
            team2_name=loser_krunker["name"],
            team2_score=loser_krunker.get("score", 0),
            team2_players=loser_players,
            team2_color=WHITE,
        )
        image_file = discord.File(img_buf, filename="scoreboard.png")
    except Exception as e:
        print(f"[Pug] scoreboard failed: {e}")

    # Post to #results
    results_msg = None
    results_ch = bot.get_channel(pug_data["config"].get("results_channel_id"))
    if results_ch:
        def _elo_line(pid):
            d = deltas.get(pid, 0)
            sign = "+" if d >= 0 else ""
            return f"<@{pid}> | **{get_elo(pid)}** ({sign}{d})"

        embed = discord.Embed(
            title=f"{match['name'].upper()} | {winner_krunker['name']}'s team wins",
            description=(
                f"**Map:** {map_name}\n"
                f"**Score:** {winner_krunker.get('score', 0)}-{loser_krunker.get('score', 0)}"
            ),
            color=0x3FB950,
        )
        embed.add_field(
            name="Winners",
            value="\n".join(_elo_line(p) for p in winner_ids),
            inline=True,
        )
        embed.add_field(
            name="Losers",
            value="\n".join(_elo_line(p) for p in loser_ids),
            inline=True,
        )
        if image_file:
            embed.set_image(url="attachment://scoreboard.png")  # render the scoreboard inside the embed
        try:
            if image_file:
                results_msg = await results_ch.send(embed=embed, file=image_file)
            else:
                results_msg = await results_ch.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[Pug] failed to post results: {e}")

    # Underperformance flags (private invite server quality control).
    try:
        await _detect_and_post_flags(bot, players, winner_krunker, loser_krunker, winner_team_num, map_name)
    except Exception as e:
        print(f"[Pug] flag detection failed (non-fatal): {e}")

    # Keep the standing big-board display current.
    try:
        from views.pug_queue import refresh_bigboard
        await refresh_bigboard(bot)
    except Exception as e:
        print(f"[Pug] bigboard refresh failed (non-fatal): {e}")

    # Announce in the match channel with a clickable link to the #results scoreboard
    scoreboard_part = ""
    if results_msg:
        scoreboard_part = f"The final scoreboard has been recorded [here]({results_msg.jump_url}).\n\n"
    teardown_embed = discord.Embed(
        description=(
            f"**{winner_krunker['name']}** has won the match. {scoreboard_part}"
            f"All text and voice channels will be deleted {teardown_ts()}."
        ),
        color=0xFFD700,
    )
    await schedule_teardown(match, bot, teardown_embed)
    return True
