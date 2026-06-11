import discord

from pug.storage import pug_matches
from views.pug_sub import RequestSubButton


# Host button
class PugHostView(discord.ui.View):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key
        self.add_item(PugHostButton(match_key))
        self.add_item(RequestSubButton(match_key))


class PugHostButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(
            label="Host Match",
            style=discord.ButtonStyle.primary,
            custom_id=f"pughost_{match_key}",
        )
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") != "host":
            await interaction.response.send_message("This match is no longer hosting.", ephemeral=True)
            return

        if interaction.user.id != match.get("host_captain_id") and match.get("sim_controller") != interaction.user.id:
            await interaction.response.send_message("Only the assigned host can use this button.", ephemeral=True)
            return

        from pug.match import build_host_url
        guild = interaction.guild
        glorp_url = build_host_url(match, "glorp", guild)
        crankshaft_url = build_host_url(match, "crankshaft", guild)

        region_name = match.get("region_name", "Dallas")
        from views.pickban import HostClientView
        view = HostClientView(glorp_url=glorp_url, crankshaft_url=crankshaft_url)
        await interaction.response.send_message(
            f"**Hosting {match['map']}**\n\n"
            f"1. Click a button to open your client\n"
            f"2. The game auto-creates with both teams\n"
            f"3. Paste the Krunker link back in this channel\n\n"
            f"Make sure your region is set to **{region_name}**.",
            view=view,
            ephemeral=True,
        )

        match["host_button_used"] = True
        await interaction.channel.send(
            embed=discord.Embed(
                description=f"<@{interaction.user.id}> is hosting **{match['map']}**...\nWaiting for the game link.",
                color=0x2B2D31,
            )
        )
