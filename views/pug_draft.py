import discord

from pug.storage import pug_matches, get_elo
from views.pug_sub import RequestSubButton


def _current_captain(match: dict) -> int | None:
    pointer = match["draft_pointer"]
    order = match["draft_order"]
    if pointer >= len(order):
        return None
    return match["captains"][order[pointer]]


def build_draft_embed(match: dict) -> discord.Embed:
    cap1, cap2 = match["captains"]
    teams = match["teams"]
    names = match.get("names", {})

    def dn(pid):
        return names.get(pid, str(pid))

    embed = discord.Embed(title=f"{match['name'].upper()} | Draft", color=0x5865F2)

    # Only captains are mentioned (top of the embed)
    embed.add_field(name="Captains", value=f"**Team 1:** <@{cap1}>\n**Team 2:** <@{cap2}>", inline=False)

    # Everyone else is shown by display name
    embed.add_field(name="Team 1", value=", ".join(dn(p) for p in teams[cap1]) or "-", inline=True)
    embed.add_field(name="Team 2", value=", ".join(dn(p) for p in teams[cap2]) or "-", inline=True)

    pool = sorted(match["pool"], key=lambda p: get_elo(p), reverse=True)
    if pool:
        embed.add_field(
            name="Available",
            value="\n".join(f"{i}. {dn(p)} ({get_elo(p)})" for i, p in enumerate(pool, start=1)),
            inline=False,
        )

    cur = _current_captain(match)
    if cur is not None:
        deadline = match.get("draft_deadline")
        timer = f" | Auto-pick <t:{int(deadline)}:R>" if deadline else " | 30s auto-pick"
        embed.set_footer(text=f"Currently picking: {dn(cur)}{timer}")
    return embed


class PugDraftView(discord.ui.View):
    def __init__(self, match_key: str):
        super().__init__(timeout=None)
        self.match_key = match_key

    def rebuild(self):
        self.clear_items()
        match = pug_matches.get(self.match_key)
        if not match:
            return
        pool = sorted(match["pool"], key=lambda p: get_elo(p), reverse=True)
        names = match.get("names", {})
        for pid in pool:
            self.add_item(DraftPickButton(self.match_key, pid, names.get(pid, str(pid))))
        self.add_item(RequestSubButton(self.match_key))


class DraftPickButton(discord.ui.Button):
    def __init__(self, match_key: str, player_id: int, name: str):
        super().__init__(
            label=f"Pick {name}"[:80],
            style=discord.ButtonStyle.success,
            custom_id=f"pugdraft_{match_key}_{player_id}",
        )
        self.match_key = match_key
        self.player_id = player_id

    async def callback(self, interaction: discord.Interaction):
        match = pug_matches.get(self.match_key)
        if not match or match.get("phase") != "draft":
            await interaction.response.send_message("This draft is no longer active.", ephemeral=True)
            return

        cur_cap = _current_captain(match)
        if interaction.user.id != cur_cap and match.get("sim_controller") != interaction.user.id:
            await interaction.response.send_message("It's not your turn to pick.", ephemeral=True)
            return
        if self.player_id not in match["pool"]:
            await interaction.response.send_message("That player was already picked.", ephemeral=True)
            return

        # A manual pick cancels the running auto-pick timer.
        task = match.get("draft_task")
        if task:
            task.cancel()

        complete = apply_pick(match, self.player_id)
        await interaction.response.defer()
        await render_after_pick(match, interaction.client, complete)


def apply_pick(match: dict, player_id: int) -> bool:
    """Assign a pick to the current captain and advance. Returns True if the draft is done."""
    cur_cap = _current_captain(match)
    match["teams"][cur_cap].append(player_id)
    match["pool"].remove(player_id)
    match["draft_pointer"] += 1
    # Auto-assign the last remaining player to the captain whose turn is next.
    if len(match["pool"]) == 1:
        last_cap = _current_captain(match)
        match["teams"][last_cap].append(match["pool"].pop(0))
        match["draft_pointer"] += 1
    return not match["pool"]


async def render_after_pick(match: dict, bot, complete: bool):
    """Re-render the draft message (used by both manual picks and the auto-pick timer)."""
    ch = bot.get_channel(match["text_channel_id"])
    msg = None
    if ch and match.get("draft_message_id"):
        try:
            msg = await ch.fetch_message(match["draft_message_id"])
        except discord.NotFound:
            pass

    if complete:
        if msg:
            await msg.edit(embed=build_draft_embed(match), view=None)
        from pug.match import start_map_vote
        await start_map_vote(match, bot)
    else:
        from pug.match import start_draft_timer
        start_draft_timer(match, bot)
        view = PugDraftView(match["key"])
        view.rebuild()
        if msg:
            await msg.edit(embed=build_draft_embed(match), view=view)
