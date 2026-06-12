import asyncio
import re

import discord

from core.bot_instance import bot
from core.config import SERVER_ID
from pug.storage import pug_matches, pug_data, pending_link_threads
from pug.match import get_match_for_channel, mark_checked_in, cleanup_orphaned_channels, reset_queue_on_start
from views.pug_queue import QueueView
from views.pug_link import AccountLinkView, LinkReviewView, post_link_request

_did_startup = False


@bot.listen("on_ready")
async def pug_on_ready():
    """Re-register persistent views, reset the stale queue embed, and clean up orphaned
    match channels. The one-time work runs only once."""
    global _did_startup
    bot.add_view(QueueView())
    bot.add_view(AccountLinkView())
    # Re-register review buttons for any pending account-link requests
    for rid in list(pug_data.get("link_requests", {}).keys()):
        bot.add_view(LinkReviewView(rid))

    if _did_startup:
        return
    _did_startup = True
    try:
        await reset_queue_on_start(bot)
        guild = bot.get_guild(SERVER_ID)
        if guild:
            n = await cleanup_orphaned_channels(guild)
            if n:
                print(f"[Pug] Cleaned up {n} orphaned match channel(s).")
    except Exception as e:
        print(f"[Pug] startup cleanup error: {e}")


@bot.listen("on_message")
async def pug_link_screenshot(message: discord.Message):
    """Capture the proof screenshot a user uploads in their private link thread."""
    if message.author.bot:
        return
    pend = pending_link_threads.get(message.channel.id)
    if not pend or message.author.id != pend["user_id"]:
        return

    img = next((a for a in message.attachments if (a.content_type or "").startswith("image")), None)
    if not img:
        await message.channel.send("Please upload an **image** screenshot to finish your request.")
        return
    try:
        data = await img.read()
    except discord.HTTPException:
        await message.channel.send("Couldn't read that image — please try uploading it again.")
        return

    rid = await post_link_request(
        bot, pend["user_id"], pend["usernames"], pend["primary"], pend["region"], data, "proof.png"
    )
    pending_link_threads.pop(message.channel.id, None)
    if not rid:
        await message.channel.send("Couldn't submit — the review channel isn't set. Ask an admin.")
        return

    await message.channel.send("✅ Submitted for review! You'll be notified once an admin reviews it.")
    await asyncio.sleep(5)
    try:
        await message.channel.delete()
    except discord.HTTPException:
        pass


@bot.listen("on_voice_state_update")
async def pug_voice_checkin(member: discord.Member, before, after):
    """Mark a player checked in when they join their match's check-in VC."""
    if member.bot or not after.channel:
        return
    for match in pug_matches.values():
        if match.get("phase") != "checkin":
            continue
        if after.channel.id == match.get("checkin_vc_id"):
            # Record where they came from (so we can return them later), if not already known
            if member.id in match.get("players", []) and member.id not in match.get("origin_vc", {}):
                match_vc_ids = {match.get("checkin_vc_id"), match.get("team1_vc_id"), match.get("team2_vc_id")}
                if before.channel and before.channel.id not in match_vc_ids:
                    match.setdefault("origin_vc", {})[member.id] = before.channel.id
            mark_checked_in(match, member.id)


@bot.listen("on_message")
async def pug_krunker_link(message: discord.Message):
    """Detect the host pasting a Krunker link in a pug match channel and post the
    join embed. Mirrors the tournament flow in events/messages.py (no casting)."""
    if message.author.bot:
        return

    match = get_match_for_channel(message.channel.id)
    if not match or match.get("phase") != "host" or not match.get("host_captain_id"):
        return

    krunker_link = re.search(r"https?://krunker\.io/\?game=\S+", message.content)
    if not krunker_link:
        return

    host_id = match["host_captain_id"]
    if message.author.id != host_id and match.get("sim_controller") != message.author.id:
        await message.delete()
        await message.channel.send(
            f"<@{message.author.id}> Only the host can post the game link. Sit tight.",
            delete_after=8,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        return

    link = krunker_link.group(0)
    want_code = match.get("region_code", "DAL")
    want_name = match.get("region_name", "Dallas").upper()
    game_code = re.search(r"\?game=(\w+):", link)
    if game_code and game_code.group(1) != want_code:
        await message.delete()
        await message.channel.send(
            embed=discord.Embed(
                title="Wrong Region",
                description=(
                    f"This match must be hosted on **{want_name}** servers.\n\n"
                    f"Set your default region to **{want_name}**, then click **Host Match** again."
                ),
                color=0xFF4444,
            ),
            delete_after=30,
        )
        return

    if not match.get("host_button_used"):
        await message.delete()
        await message.channel.send(
            embed=discord.Embed(
                title="Use the Host Button",
                description="Click the **Host Match** button first, then paste the link.",
                color=0xFF4444,
            ),
            delete_after=15,
        )
        return

    match["host_button_used"] = False

    cap1, cap2 = match["captains"]
    all_ids = match["teams"][cap1] + match["teams"][cap2]
    all_pings = " ".join(f"<@{p}>" for p in all_ids)

    from pug.storage import primary_username
    join_embed = discord.Embed(
        title="Join the Game",
        description=(
            f"{link}\n\n"
            f"**{primary_username(cap1, 'Team 1')}** → join **Alpha** (Team 1)\n"
            f"**{primary_username(cap2, 'Team 2')}** → join **Beta** (Team 2)"
        ),
        color=0x00FF7F,
    )
    join_embed.set_footer(text="Join the correct team or the match won't count.")
    await message.channel.send(
        all_pings,
        embed=join_embed,
        allowed_mentions=discord.AllowedMentions(users=True),
    )
