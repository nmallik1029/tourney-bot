import discord

from pug.storage import pug_matches
from pug.config import member_is_pug_staff


class CheckinView(discord.ui.View):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key
        self.add_item(CheckInButton(match_key))
        self.add_item(AbortButton(match_key))


class CheckInButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(label="Check In", style=discord.ButtonStyle.success, custom_id=f"pugcheckin_{match_key}")
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") != "checkin":
            await interaction.response.send_message("Check-in is no longer active.", ephemeral=True)
            return
        if interaction.user.id not in match.get("players", []):
            await interaction.response.send_message("You're not in this match.", ephemeral=True)
            return
        from pug.match import mark_checked_in
        mark_checked_in(match, interaction.user.id)
        await interaction.response.send_message("You're checked in.", ephemeral=True)


class AbortButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(label="Abort", style=discord.ButtonStyle.danger, custom_id=f"pugabort_{match_key}")
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") != "checkin":
            await interaction.response.send_message("Check-in is no longer active.", ephemeral=True)
            return
        if interaction.user.id not in match.get("players", []) and not member_is_pug_staff(interaction.user):
            await interaction.response.send_message(
                "Only a player in this match (or staff) can abort.", ephemeral=True
            )
            return
        await interaction.response.send_message("Check-in aborted.", ephemeral=True)
        from pug.match import abort_checkin
        await abort_checkin(match, interaction.client, interaction.user)
