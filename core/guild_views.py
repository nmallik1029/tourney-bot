"""Base classes that bind the per-guild context for component interactions.

discord.py calls `interaction_check` immediately before an item callback (and before
a modal's `on_submit`), in the same coroutine/context. That's the one reliable place
to set the current guild for button/select/modal clicks, which -- unlike slash commands
-- don't pass through the command tree's check. Every view and modal that touches
per-guild data should subclass these instead of discord.ui.View / discord.ui.Modal.
"""

import discord

from core.guild_ctx import set_guild


class GuildView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is not None:
            set_guild(interaction.guild_id)
        return True


class GuildModal(discord.ui.Modal):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is not None:
            set_guild(interaction.guild_id)
        return True
