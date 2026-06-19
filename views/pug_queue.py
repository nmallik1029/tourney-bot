import discord

from pug.config import MATCH_SIZE, BRAND
from pug.storage import (
    pug_data,
    pug_queue,
    get_player,
    is_noadded,
    get_noadd_info,
    top_rated_players,
)


def _account_link_mention() -> str:
    """Clickable #channel mention if configured, else a plain fallback."""
    cid = pug_data["config"].get("account_link_channel_id")
    return f"<#{cid}>" if cid else "**#account-link**"


def build_queue_embed() -> discord.Embed:
    """The persistent embed in #queue, showing who's currently queued."""
    count = len(pug_queue)
    embed = discord.Embed(
        title="Competitive Krunker League Queue",
        description=f"**{count}/{MATCH_SIZE}** in queue",
        color=0x5865F2 if count < MATCH_SIZE else 0x3FB950,
    )

    # Players (left column).
    if pug_queue:
        lines = []
        for i, pid in enumerate(pug_queue, start=1):
            names = ", ".join(get_player(pid)["usernames"]) or "-"
            lines.append(f"`{i}.` <@{pid}> | {names}")
        embed.add_field(name="Players", value="\n".join(lines), inline=True)
    else:
        embed.add_field(name="Players", value="*Queue is empty*", inline=True)

    # Top players by average CKL rating (right column).
    top = top_rated_players(limit=5)
    rating_value = "\n".join(
        f"`{i}.` <@{did}> | **{avg}**" for i, (did, avg, games) in enumerate(top, start=1)
    ) if top else "*No rated players yet*"
    embed.add_field(name="Top CKL Rated", value=rating_value, inline=True)

    embed.set_footer(text="Click Join to queue")
    return embed


async def refresh_queue_embed(bot):
    """Re-render the persistent queue message in #queue."""
    cfg = pug_data["config"]
    ch = bot.get_channel(cfg.get("queue_channel_id"))
    if not ch:
        return
    msg_id = cfg.get("queue_message_id")
    if not msg_id:
        return
    try:
        msg = await ch.fetch_message(msg_id)
        await msg.edit(embed=build_queue_embed(), view=QueueView())
    except discord.NotFound:
        pass


class QueueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join Queue", style=discord.ButtonStyle.success, custom_id="pug_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id

        if is_noadded(uid):
            info = get_noadd_info(uid) or {}
            until = info.get("until")
            reason = info.get("reason") or "*No reason provided.*"
            if until:
                expiry = f"Your no-add expires <t:{int(until)}:R> (<t:{int(until)}:f>)."
            else:
                expiry = "Your no-add is **permanent** until an admin removes it."
            await interaction.response.send_message(
                f"You are currently **noadded** and can't queue.\n"
                f"{expiry}\n"
                f"**Reason:** {reason}",
                ephemeral=True,
            )
            return
        if not get_player(uid)["usernames"]:
            await interaction.response.send_message(
                "You need a **linked Krunker account** before you can queue.\n"
                f"Post your username + proof in {_account_link_mention()} to get access to the queue.",
                ephemeral=True,
            )
            return
        if uid in pug_queue:
            await interaction.response.send_message("You're already in the queue.", ephemeral=True)
            return

        # Don't let players who are already in an active (popped) match re-queue.
        from pug.storage import in_active_match
        if in_active_match(uid):
            await interaction.response.send_message(
                "You're already in an active match.", ephemeral=True
            )
            return

        get_player(uid)  # ensure a record exists
        pug_queue.append(uid)

        if len(pug_queue) >= MATCH_SIZE:
            # Pop the queue. pop_queue takes the players and refreshes the embed.
            from pug.match import pop_queue
            await interaction.response.send_message(
                "You joined the queue.", ephemeral=True
            )
            await pop_queue(interaction.guild, interaction.client)
        else:
            await interaction.response.send_message(
                f"You joined the queue ({len(pug_queue)}/{MATCH_SIZE}).", ephemeral=True
            )
            await interaction.message.edit(embed=build_queue_embed(), view=self)

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.secondary, custom_id="pug_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in pug_queue:
            await interaction.response.send_message("You're not in the queue.", ephemeral=True)
            return
        pug_queue.remove(uid)
        await interaction.response.send_message("You left the queue.", ephemeral=True)
        await interaction.message.edit(embed=build_queue_embed(), view=self)

    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.primary, custom_id="pug_leaderboard")
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed, total_pages = build_leaderboard_embed(0)
        view = LeaderboardView(0) if total_pages > 1 else None
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


LEADERBOARD_PER_PAGE = 10


def _ranked_players() -> list:
    """Players who have actually played a game (>=1 win or loss), sorted by ELO desc."""
    players = pug_data["players"]
    played = [
        (did, p) for did, p in players.items()
        if (p.get("wins", 0) + p.get("losses", 0)) > 0
    ]
    return sorted(played, key=lambda kv: kv[1].get("elo", 0), reverse=True)


def build_leaderboard_embed(page: int = 0) -> tuple[discord.Embed, int]:
    """Return (embed, total_pages) for the given 0-indexed page, 10 players per page."""
    ranked = _ranked_players()
    total_pages = max(1, (len(ranked) + LEADERBOARD_PER_PAGE - 1) // LEADERBOARD_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    if not ranked:
        return (
            discord.Embed(
                title=f"{BRAND} Leaderboard",
                description="*No ranked players yet. Play a game to get on the board.*",
                color=0xF1C40F,
            ),
            total_pages,
        )

    start = page * LEADERBOARD_PER_PAGE
    chunk = ranked[start:start + LEADERBOARD_PER_PAGE]

    lines = []
    for offset, (did, p) in enumerate(chunk):
        rank = f"`{start + offset + 1}.`"
        w = p.get("wins", 0)
        l = p.get("losses", 0)
        lines.append(f"{rank} <@{did}> | **{p.get('elo', 0)}** ({w}W/{l}L)")

    embed = discord.Embed(
        title=f"{BRAND} Leaderboard",
        description="\n".join(lines),
        color=0xF1C40F,
    )
    embed.set_footer(text=f"Page {page + 1}/{total_pages} | {len(ranked)} ranked players")
    return embed, total_pages


class LeaderboardView(discord.ui.View):
    """Prev/Next paging for the leaderboard. Short-lived (not persistent across restarts)."""

    def __init__(self, page: int = 0):
        super().__init__(timeout=180)
        self.page = page
        self._sync()

    def _sync(self):
        _, total_pages = build_leaderboard_embed(self.page)
        self.total_pages = total_pages
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= total_pages - 1

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync()
        embed, _ = build_leaderboard_embed(self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.page + 1
        self._sync()
        embed, _ = build_leaderboard_embed(self.page)
        await interaction.response.edit_message(embed=embed, view=self)
