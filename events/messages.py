import re
import discord
from core.bot_instance import bot
from core.config import CASTER_ROLE_ID
from core.storage import tournaments, active_matches
from views.pickban import PickBanView


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
                await message.channel.send(
                    f"<@{message.author.id}> Only the host can post game links. Sit tight!",
                    delete_after=8,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                return

            # Host captain posted a link -- check it's NY region
            link = krunker_link.group(0)
            game_code = re.search(r"\?game=(\w+):", link)
            if game_code and game_code.group(1) != "NY":
                await message.delete()
                embed = discord.Embed(
                    title="Wrong Region",
                    description=(
                        "Your game is not on **New York** servers.\n\n"
                        "**Fix it:**\n"
                        "1. Set your default region to **New York** in Krunker settings\n"
                        "2. Click the **Host** button again\n\n"
                        "Still stuck? Ask <@723538323636748359>"
                    ),
                    color=0xFF4444,
                )
                await message.channel.send(
                    embed=embed,
                    delete_after=30,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                return

            # Reject links not launched through the host button
            if not match_for_channel.get("host_button_used"):
                await message.delete()
                embed = discord.Embed(
                    title="Use the Host Button",
                    description=(
                        "Don't host manually -- the bot needs to set up tracking.\n\n"
                        "**Click the Host button**, then paste the link."
                    ),
                    color=0xFF4444,
                )
                await message.channel.send(embed=embed, delete_after=15)
                return

            match_for_channel["host_button_used"] = False

            # Valid NY link -- check if this match is being casted
            team1 = match_for_channel["teams"][0]
            team2 = match_for_channel["teams"][1]
            all_pings = " ".join(
                f"<@{pid}>" for pid in team1.get("player_ids", []) + team2.get("player_ids", [])
            )
            map_idx = match_for_channel.get("current_map_index", 0)
            all_maps = match_for_channel.get("all_maps", [])
            current_map = all_maps[map_idx] if map_idx < len(all_maps) else "Unknown"

            if match_for_channel.get("casted"):
                # Delete the link so players can't join early
                await message.delete()

                # Send link to caster-links channel
                t_id = match_for_channel.get("tournament_id")
                t = tournaments.get(t_id, {})
                caster_ch = bot.get_channel(t.get("caster_channel_id")) if t else None

                if caster_ch:
                    caster_role = message.guild.get_role(CASTER_ROLE_ID)
                    caster_ping = caster_role.mention if caster_role else ""

                    caster_embed = discord.Embed(
                        title=f"{team1['name']} vs {team2['name']} -- Map {map_idx + 1}: {current_map}",
                        description=f"**Link:** {link}",
                        color=0xFFA500,
                    )
                    caster_embed.set_footer(text="React \u2705 when the caster is ready to release the link to players.")

                    caster_msg = await caster_ch.send(
                        caster_ping,
                        embed=caster_embed,
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
                    await caster_msg.add_reaction("\u2705")

                    match_for_channel["pending_caster_link"] = {
                        "link": link,
                        "caster_message_id": caster_msg.id,
                        "caster_channel_id": caster_ch.id,
                    }

                # Tell the match channel to wait
                wait_embed = discord.Embed(
                    title="\U0001f3a5 This match is being casted!",
                    description="The link has been sent to the casting team.\nPlease wait for the link to be released.",
                    color=0xFFA500,
                )
                await message.channel.send(embed=wait_embed)
                return

            # Not casted -- send link directly to players
            join_embed = discord.Embed(
                title="Step 3/3 -- Join the Game",
                description=(
                    f"{link}\n\n"
                    f"**{team1['name']}** -> join **Alpha** (Team 1)\n"
                    f"**{team2['name']}** -> join **Beta** (Team 2)"
                ),
                color=0x00FF7F,
            )
            join_embed.set_footer(text="Join the correct team or the match won't count.")

            await message.channel.send(
                f"{all_pings}",
                embed=join_embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            return

        # Non-link message in a match channel during hosting -- re-send host buttons at the bottom
        if not krunker_link and match_for_channel.get("host_message_id"):
            ch = message.channel
            try:
                old_msg = await ch.fetch_message(match_for_channel["host_message_id"])
                await old_msg.delete()
            except discord.NotFound:
                pass

            match_id = match_for_channel["match_id"]
            host_captain_id = match_for_channel["host_captain_id"]
            map_idx = match_for_channel.get("current_map_index", 0)
            all_maps = match_for_channel.get("all_maps", [])
            current_map = all_maps[map_idx] if map_idx < len(all_maps) else "Unknown"

            host_embed = discord.Embed(
                title="Step 2/3 -- Host the Match",
                description=(
                    f"<@{host_captain_id}>, click the button below to host **Map {map_idx + 1}: {current_map}**."
                ),
                color=0xFFA500,
            )
            host_embed.set_footer(text="Everyone else: hang tight until the game link is posted.")

            host_view = PickBanView(match_id=match_id)
            host_view.rebuild()
            host_msg = await ch.send(
                embed=host_embed,
                view=host_view,
            )
            match_for_channel["host_message_id"] = host_msg.id
