import discord
from core.storage import tournaments, save_tournaments


class VODSubmitModal(discord.ui.Modal, title="Submit VOD"):
    vod_link = discord.ui.TextInput(
        label="VOD Link",
        placeholder="Paste your VOD link (YouTube, Medal, etc.)",
        max_length=500,
    )

    def __init__(self, tournament_id: str, team_id: str, player_ign: str, player_discord_id: int):
        super().__init__()
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.player_ign = player_ign
        self.player_discord_id = player_discord_id

    async def on_submit(self, interaction: discord.Interaction):
        t = tournaments.get(self.tournament_id)
        if not t:
            await interaction.response.send_message("Tournament not found.", ephemeral=True)
            return

        team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("Team not found.", ephemeral=True)
            return

        # Store the VOD submission
        if "vod_submissions" not in team:
            team["vod_submissions"] = {}
        team["vod_submissions"][str(self.player_discord_id)] = {
            "ign": self.player_ign,
            "link": self.vod_link.value.strip(),
        }
        save_tournaments()

        await interaction.response.send_message(
            f"**{self.player_ign}**, your VOD has been submitted!\n"
            f"Link: {self.vod_link.value.strip()}",
            ephemeral=True,
        )

        # Update the original embed to show who has submitted
        try:
            # Disable the button for this player
            for item in interaction.message.components[0].children if interaction.message.components else []:
                pass  # Can't modify components from modal callback easily

            # Send a visible confirmation in channel
            await interaction.channel.send(
                embed=discord.Embed(
                    description=f"\u2705 **{self.player_ign}** has submitted their VOD.",
                    color=0x00FF7F,
                )
            )
        except Exception:
            pass


class VODSubmitButton(discord.ui.Button):
    def __init__(self, tournament_id: str, team_id: str, player_ign: str, player_discord_id: int):
        super().__init__(
            label=f"Submit VOD - {player_ign}",
            style=discord.ButtonStyle.primary,
            custom_id=f"vod_{tournament_id}_{team_id}_{player_discord_id}",
        )
        self.tournament_id = tournament_id
        self.team_id = team_id
        self.player_ign = player_ign
        self.player_discord_id = player_discord_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.player_discord_id:
            await interaction.response.send_message("Only click your own button.", ephemeral=True)
            return

        # Check if already submitted
        t = tournaments.get(self.tournament_id)
        if t:
            team = next((tm for tm in t["teams"] if tm["team_id"] == self.team_id), None)
            if team and str(self.player_discord_id) in team.get("vod_submissions", {}):
                await interaction.response.send_message(
                    "You've already submitted your VOD! If you need to resubmit, contact a tournament manager.",
                    ephemeral=True,
                )
                return

        modal = VODSubmitModal(
            tournament_id=self.tournament_id,
            team_id=self.team_id,
            player_ign=self.player_ign,
            player_discord_id=self.player_discord_id,
        )
        await interaction.response.send_modal(modal)


class VODSubmissionView(discord.ui.View):
    def __init__(self, tournament_id: str, team_id: str, players: list[dict]):
        super().__init__(timeout=None)
        for player in players:
            self.add_item(VODSubmitButton(
                tournament_id=tournament_id,
                team_id=team_id,
                player_ign=player["ign"],
                player_discord_id=player["discord_id"],
            ))
