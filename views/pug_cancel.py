import discord

from pug.storage import pug_matches
from pug.config import member_is_pug_staff


class CancelRequestView(discord.ui.View):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key
        self.add_item(CancelAgreeButton(match_key))
        self.add_item(CancelDenyButton(match_key))


class CancelAgreeButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(label="Agree to Cancel", style=discord.ButtonStyle.danger, custom_id=f"pugcancelok_{match_key}")
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        pc = match.get("pending_cancel") if match else None
        if not pc:
            await interaction.response.send_message("No pending cancellation.", ephemeral=True)
            return

        caps = match.get("captains", [])
        is_staff = member_is_pug_staff(interaction.user)
        if interaction.user.id not in caps and not is_staff:
            await interaction.response.send_message("Only a captain or admin can agree.", ephemeral=True)
            return

        pc["approved"].add(interaction.user.id)

        # Finalize if both captains agreed, or an admin/staff forced it.
        if set(caps).issubset(pc["approved"]) or is_staff:
            match.pop("pending_cancel", None)
            for item in self.view.children:
                item.disabled = True
            await interaction.response.edit_message(content="Cancellation agreed.", embed=None, view=self.view)
            from pug.match import cancel_match
            await cancel_match(match, interaction.client, headline="Both captains agreed to cancel this match.")
        else:
            remaining = [c for c in caps if c not in pc["approved"]]
            await interaction.response.edit_message(view=self.view)
            await interaction.followup.send(
                f"<@{interaction.user.id}> agreed to cancel. Waiting on "
                f"{' '.join(f'<@{c}>' for c in remaining)}.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )


class CancelDenyButton(discord.ui.Button):
    def __init__(self, match_key: str):
        super().__init__(label="Keep Playing", style=discord.ButtonStyle.secondary, custom_id=f"pugcancelno_{match_key}")
        self.match_key = match_key

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        pc = match.get("pending_cancel") if match else None
        if not pc:
            await interaction.response.send_message("No pending cancellation.", ephemeral=True)
            return
        if interaction.user.id not in match.get("captains", []) and not member_is_pug_staff(interaction.user):
            await interaction.response.send_message("Only a captain or admin can respond.", ephemeral=True)
            return

        match.pop("pending_cancel", None)
        for item in self.view.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Cancellation declined by <@{interaction.user.id}>, match continues.",
            embed=None, view=self.view,
        )
