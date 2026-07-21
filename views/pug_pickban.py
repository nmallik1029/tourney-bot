import discord

from core.config import SET_REGION_NAME
from core.guild_views import GuildView
from pug.config import member_is_pug_staff
from pug.storage import pug_matches
from views.pug_sub import RequestSubButton


async def send_host_launch(interaction: discord.Interaction, match: dict, *, allow_staff=False, rehost=False):
    """Shared host/rehost flow: give the launch links to whoever wants to host, re-arm
    link detection. Anyone in the match (or staff) can host; the first valid link wins."""
    is_player = interaction.user.id in match.get("players", []) or match.get("sim_controller") == interaction.user.id
    if not is_player and not member_is_pug_staff(interaction.user):
        await interaction.response.send_message("Only players in this match (or staff) can host.", ephemeral=True)
        return

    from pug.match import build_host_url
    glorp_url = build_host_url(match, "glorp", interaction.guild)
    crankshaft_url = build_host_url(match, "crankshaft", interaction.guild)
    kcc_url = build_host_url(match, "kcc", interaction.guild)
    region_name = match.get("region_name", SET_REGION_NAME)

    from views.pickban import HostClientView
    view = HostClientView(glorp_url=glorp_url, crankshaft_url=crankshaft_url, kcc_url=kcc_url)
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
    if rehost:
        await interaction.channel.send(
            embed=discord.Embed(
                description=(
                    f"**Rehost requested** by <@{interaction.user.id}>.\n"
                    f"Anyone: create a new **{match['map']}** lobby and paste the new link here."
                ),
                color=0xFFA500,
            ),
        )
    else:
        await interaction.channel.send(
            embed=discord.Embed(
                description=f"<@{interaction.user.id}> is hosting **{match['map']}**...\nWaiting for the game link.",
                color=0x2B2D31,
            )
        )


# ── Host (Step: Click to Host) ──────────────────────────────────────────────────
class PugHostView(GuildView):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key
        self.add_item(PugHostButton(match_key))
        self.add_item(RequestSubButton(match_key))


class PugHostButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(label="Host Match", style=discord.ButtonStyle.primary, custom_id=f"pughost_{match_key}")
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") != "host":
            await interaction.response.send_message("This match is no longer hosting.", ephemeral=True)
            return
        await send_host_launch(interaction, match)


# ── Join-the-game view (posted with the link) — has Rehost + Request a Sub ───────
class JoinGameView(GuildView):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key
        self.add_item(RehostButton(match_key))
        self.add_item(RequestSubButton(match_key))


class RehostButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(label="Rehost Map", style=discord.ButtonStyle.secondary, custom_id=f"pugrehost_{match_key}")
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") != "host":
            await interaction.response.send_message("This match is no longer active.", ephemeral=True)
            return
        await send_host_launch(interaction, match, allow_staff=True, rehost=True)
