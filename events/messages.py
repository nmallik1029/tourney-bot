import re
import discord
from core.bot_instance import bot
from core.config import CASTER_ROLE_ID
from core.storage import tournaments, active_matches
from views.pickban import PickBanView


class CasterReleaseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Release Link", style=discord.ButtonStyle.success, custom_id="caster_release_link")
    async def release_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = next(
            (
                m for m in active_matches.values()
                if m.get("pending_caster_link", {}).get("caster_message_id") == interaction.message.id
                and (not m.get("guild_id") or m.get("guild_id") == interaction.guild.id)
            ),
            None,
        )
        if not match:
            await interaction.response.send_message("No pending caster link found.", ephemeral=True)
            return

        from core.guild_config import caster_role_id
        caster_role = (
            interaction.guild.get_role(caster_role_id(interaction.guild.id))
            if interaction.guild else None
        )
        if (
            not interaction.user.guild_permissions.administrator
            and (not caster_role or caster_role not in interaction.user.roles)
        ):
            await interaction.response.send_message("Only casters or admins can release this link.", ephemeral=True)
            return

        pending = match["pending_caster_link"]
        match_ch = bot.get_channel(match.get("channel_id"))
        if not match_ch:
            await interaction.response.send_message("Match channel not found.", ephemeral=True)
            return

        team1 = match["teams"][0]
        team2 = match["teams"][1]
        all_pings = " ".join(
            f"<@{pid}>" for pid in team1.get("player_ids", []) + team2.get("player_ids", [])
        )

        join_embed = discord.Embed(
            title="Step 3/3 -- Join the Game",
            description=(
                f"{pending['link']}\n\n"
                f"**{team1['name']}** -> join **Alpha** (Team 1)\n"
                f"**{team2['name']}** -> join **Beta** (Team 2)"
            ),
            color=0x00FF7F,
        )
        join_embed.set_footer(text="Join the correct team or the match won't count.")

        await match_ch.send(
            all_pings,
            embed=join_embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        match.pop("pending_caster_link", None)

        released_embed = interaction.message.embeds[0].copy() if interaction.message.embeds else discord.Embed()
        released_embed.color = 0x00FF7F
        released_embed.set_footer(text=f"Released by {interaction.user.display_name}")
        button.disabled = True
        await interaction.response.edit_message(embed=released_embed, view=self)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # Krunker link handling in pick/ban match channels
    match_for_channel = next(
        (
            m for m in active_matches.values()
            if m.get("channel_id") == message.channel.id
            and (not m.get("guild_id") or m.get("guild_id") == message.guild.id)
        ),
        None,
    )
    if match_for_channel and match_for_channel.get("host_captain_id"):
        krunker_link = re.search(r"https?://krunker\.io/\?game=\S+", message.content)
        if krunker_link:
            host_id = match_for_channel["host_captain_id"]
            if message.author.id != host_id:
                await message.delete()
                await message.channel.send(
                    f"<@{message.author.id}> Only the host can post game links. Sit tight.",
                    delete_after=8,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                return

            # Host captain posted a link -- check it's DAL region
            link = krunker_link.group(0)
            game_code = re.search(r"\?game=(\w+):", link)
            if game_code and game_code.group(1) != "DAL":
                await message.delete()
                embed = discord.Embed(
                    title="Wrong Region",
                    description=(
                        "Your game is not on **DALLAS** servers.\n\n"
                        "**Fix it:**\n"
                        "1. Set your default region to **DALLAS** in Krunker settings\n"
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
            # The host button increments current_map_index AFTER hosting, so the map
            # actually being played is one less than the stored value.
            next_map_idx = match_for_channel.get("current_map_index", 0)
            map_idx = max(0, next_map_idx - 1)
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
                    from core.guild_config import caster_role_id
                    caster_role = message.guild.get_role(caster_role_id(message.guild.id))
                    caster_ping = caster_role.mention if caster_role else ""

                    caster_embed = discord.Embed(
                        title=f"{team1['name']} vs {team2['name']} -- Map {map_idx + 1}: {current_map}",
                        description=f"**Link:** {link}",
                        color=0xFFA500,
                    )
                    caster_embed.set_footer(text="Use Release Link when the caster is ready to send the link to players.")

                    caster_msg = await caster_ch.send(
                        caster_ping,
                        embed=caster_embed,
                        allowed_mentions=discord.AllowedMentions(roles=True),
                        view=CasterReleaseView(),
                    )

                    match_for_channel["pending_caster_link"] = {
                        "link": link,
                        "caster_message_id": caster_msg.id,
                        "caster_channel_id": caster_ch.id,
                    }

                # Tell the match channel to wait
                wait_embed = discord.Embed(
                    title="This match is being casted",
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
