import discord

from core.config import MAPS
from core.guild_views import GuildView
from pug.storage import pug_matches
from views.pug_sub import RequestSubButton


def build_vote_embed(match: dict, final: bool = False) -> discord.Embed:
    votes = match.get("votes", {})
    names = match.get("names", {})

    def dn(pid):
        return names.get(pid, str(pid))

    lines = []
    for m in MAPS:
        voters = [pid for pid, choice in votes.items() if choice == m]
        voter_str = ", ".join(dn(p) for p in voters)
        lines.append(f"**{m}** | {voter_str}")

    not_voted = [p for p in match["players"] if p not in votes]
    body = "\n".join(lines)
    if not_voted:
        body += "\n\nNot voted: " + ", ".join(dn(p) for p in not_voted)

    embed = discord.Embed(title=f"{match['name'].upper()} | Map Vote", color=0x2B2D31)
    if final:
        embed.description = f"Map selected: **{match.get('map', '?')}**\n\n{body}"
    else:
        deadline = int(match.get("vote_deadline", 0))
        embed.description = f"Voting ends <t:{deadline}:R>, click a map to vote.\n\n{body}"
    return embed


class MapVoteView(GuildView):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key
        for m in MAPS:
            self.add_item(MapVoteButton(match_key, m))
        self.add_item(RequestSubButton(match_key))


class MapVoteButton(discord.ui.Button):
    def __init__(self, match_key: str, map_name: str):
        super().__init__(
            label=map_name,
            style=discord.ButtonStyle.secondary,
            custom_id=f"pugvote_{match_key}_{map_name}",
        )
        self.match_key = match_key
        self.map_name = map_name

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") != "vote":
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        if interaction.user.id not in match["players"]:
            await interaction.response.send_message("You're not in this match.", ephemeral=True)
            return

        votes = match["votes"]
        uid = interaction.user.id
        if votes.get(uid) == self.map_name:
            del votes[uid]            # toggle off
        else:
            votes[uid] = self.map_name  # set / move vote

        await interaction.response.edit_message(
            embed=build_vote_embed(match),
            view=MapVoteView(self.match_key),
        )
