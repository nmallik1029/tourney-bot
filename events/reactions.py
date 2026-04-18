import discord
from core.bot_instance import bot
from core.config import ROLE_ID
from core.storage import active_matches


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if str(payload.emoji) != "\u2705":
        return

    # Check if this reaction is on a pending caster link message
    for match in active_matches.values():
        pending = match.get("pending_caster_link")
        if not pending:
            continue
        if pending["caster_message_id"] != payload.message_id:
            continue

        # Verify the reactor has the staff role
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            break
        member = guild.get_member(payload.user_id)
        if not member:
            break
        staff_role = guild.get_role(ROLE_ID)
        if not member.guild_permissions.administrator and (not staff_role or staff_role not in member.roles):
            break

        # Release the link to the match channel
        match_ch = bot.get_channel(match.get("channel_id"))
        if not match_ch:
            break

        link = pending["link"]
        team1 = match["teams"][0]
        team2 = match["teams"][1]
        all_pings = " ".join(
            f"<@{pid}>" for pid in team1.get("player_ids", []) + team2.get("player_ids", [])
        )

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

        await match_ch.send(
            f"{all_pings}",
            embed=join_embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

        # Clean up
        match.pop("pending_caster_link", None)

        # Update caster message to show it was released
        try:
            caster_ch = bot.get_channel(pending["caster_channel_id"])
            caster_msg = await caster_ch.fetch_message(pending["caster_message_id"])
            released_embed = caster_msg.embeds[0].copy()
            released_embed.color = 0x00FF7F
            released_embed.set_footer(text=f"\u2705 Released by {member.display_name}")
            await caster_msg.edit(embed=released_embed)
        except Exception:
            pass

        break
