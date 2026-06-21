import discord

from pug.config import MATCH_SIZE, BRAND
from pug.storage import (
    pug_data,
    pug_queue,
    get_player,
    is_noadded,
    get_noadd_info,
    save_pug_data,
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

    if pug_queue:
        lines = []
        for i, pid in enumerate(pug_queue, start=1):
            names = ", ".join(get_player(pid)["usernames"]) or "-"
            lines.append(f"`{i}.` <@{pid}> | {names}")
        embed.add_field(name="Players", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Players", value="*Queue is empty*", inline=False)

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
        embed, _, _ = build_normal_leaderboard("elo", 0)
        await interaction.response.send_message(embed=embed, view=LeaderboardView("elo", 0), ephemeral=True)


# ── Multi-stat leaderboard ───────────────────────────────────────────────────────
def _kd(p):
    k, d = p.get("kills", 0), p.get("deaths", 0)
    return k / d if d else float(k)


def _winrate(p):
    w, l = p.get("wins", 0), p.get("losses", 0)
    return (w / (w + l)) if (w + l) else 0.0


def _avg_rating(p):
    g = p.get("rating_games", 0)
    return (p.get("rating_sum", 0.0) / g) if g else 0.0


def _played_rating(p):
    return p.get("rating_games", 0) > 0


def _avg_obj(p):
    g = p.get("games", 0)
    return (p.get("obj", 0) / g) if g else 0.0


def _played_ranked(p):
    return (p.get("wins", 0) + p.get("losses", 0)) > 0


def _played_stats(p):
    return p.get("games", 0) > 0


# key -> (label, sort_value, full_display, short_display, eligible)
STATS = {
    "elo":    ("ELO",          lambda p: p.get("elo", 0),
               lambda p: f"**{p.get('elo',0)}** ({p.get('wins',0)}W/{p.get('losses',0)}L)",
               lambda p: f"**{p.get('elo',0)}**", _played_ranked),
    "wl":     ("Win Rate",     _winrate,
               lambda p: f"**{round(_winrate(p)*100)}%** ({p.get('wins',0)}W/{p.get('losses',0)}L)",
               lambda p: f"**{round(_winrate(p)*100)}%**", _played_ranked),
    # Games played = all-time ranked games (wins + losses), so historical games count
    # too. (The tracked-only `games` field is just the denominator for averages.)
    "games":  ("Games Played", lambda p: p.get("wins", 0) + p.get("losses", 0),
               lambda p: f"**{p.get('wins',0)+p.get('losses',0)}** games",
               lambda p: f"**{p.get('wins',0)+p.get('losses',0)}**", _played_ranked),
    "rating": ("CKL Rating",   _avg_rating,
               lambda p: f"**{round(_avg_rating(p),2)}** / 10",
               lambda p: f"**{round(_avg_rating(p),2)}**", _played_rating),
    "kd":     ("K/D",          _kd,
               lambda p: f"**{_kd(p):.2f}** ({p.get('kills',0)}/{p.get('deaths',0)})",
               lambda p: f"**{_kd(p):.2f}**", _played_stats),
    "obj":    ("Avg OBJ",      _avg_obj,
               lambda p: f"**{round(_avg_obj(p))}** obj",
               lambda p: f"**{round(_avg_obj(p))}**", _played_stats),
}
STAT_ORDER = ["elo", "wl", "games", "rating", "kd", "obj"]


def _ranked_for_stat(stat_key: str) -> list:
    _label, value_fn, _full, _short, eligible = STATS[stat_key]
    out = [(int(did), p) for did, p in pug_data["players"].items() if eligible(p)]
    out.sort(key=lambda t: value_fn(t[1]), reverse=True)
    return out


def build_stat_leaderboard(stat_key="elo", start=0, count=10, columns=1):
    """Build a leaderboard embed for one stat. Returns (embed, total_eligible).
    With columns>1 the `count` players are split into that many inline columns."""
    if stat_key not in STATS:
        stat_key = "elo"
    label, _v, full_disp, short_disp, _e = STATS[stat_key]
    ranked = _ranked_for_stat(stat_key)
    total = len(ranked)
    chunk = ranked[start:start + count]

    embed = discord.Embed(title=f"{BRAND} Leaderboard: {label}", color=0xF1C40F)
    if not chunk:
        embed.description = "*No ranked players yet. Play a game to get on the board.*"
        return embed, total

    if columns <= 1:
        lines = [f"`{start+i+1}.` <@{did}> | {full_disp(p)}" for i, (did, p) in enumerate(chunk)]
        embed.description = "\n".join(lines)
    else:
        # Spread the page's players evenly across the columns so they all sit on one
        # row (Discord renders at most 3 inline fields per row) with no stray column.
        per_col = max(1, (len(chunk) + columns - 1) // columns)
        for c in range(columns):
            seg = chunk[c*per_col:(c+1)*per_col]
            if not seg:
                break
            base = start + c*per_col
            val = "\n".join(f"`{base+j+1}.` <@{did}> | {short_disp(p)}" for j, (did, p) in enumerate(seg))
            embed.add_field(name="​", value=val, inline=True)  # blank header (ranks are in the lines)
    return embed, total


SHORT_LABELS = {"elo": "ELO", "wl": "W/L", "games": "Games", "rating": "Rating", "kd": "K/D", "obj": "OBJ"}
NORMAL_PER_PAGE = 10


# ── Normal leaderboard (anyone): top 10 per page, compact stat buttons + paging ──
def build_normal_leaderboard(stat="elo", page=0):
    """Returns (embed, clamped_page, total_pages) for the 10-per-page leaderboard."""
    if stat not in STATS:
        stat = "elo"
    total = len(_ranked_for_stat(stat))
    pages = max(1, (total + NORMAL_PER_PAGE - 1) // NORMAL_PER_PAGE)
    page = max(0, min(page, pages - 1))
    embed, _ = build_stat_leaderboard(stat, page * NORMAL_PER_PAGE, NORMAL_PER_PAGE, columns=1)
    embed.set_footer(text=f"Page {page+1}/{pages} | {total} ranked players")
    return embed, page, pages


def _next_stat(stat):
    return STAT_ORDER[(STAT_ORDER.index(stat) + 1) % len(STAT_ORDER)]


class LeaderboardView(discord.ui.View):
    """Ephemeral leaderboard: one stat-cycle button + Prev/Next paging (single row)."""

    def __init__(self, stat="elo", page=0):
        super().__init__(timeout=180)
        self.stat = stat if stat in STATS else "elo"
        _, self.page, self.pages = build_normal_leaderboard(self.stat, page)

        self.add_item(self._cycle_btn())
        self.add_item(self._page_btn("◀ Prev", -1, self.page <= 0))
        self.add_item(self._page_btn("Next ▶", +1, self.page >= self.pages - 1))

    def _cycle_btn(self):
        btn = discord.ui.Button(label=f"Stat: {SHORT_LABELS[self.stat]}", style=discord.ButtonStyle.primary, row=0)
        nxt = _next_stat(self.stat)

        async def cb(interaction: discord.Interaction):
            embed, _, _ = build_normal_leaderboard(nxt, 0)
            await interaction.response.edit_message(embed=embed, view=LeaderboardView(nxt, 0))

        btn.callback = cb
        return btn

    def _page_btn(self, label, delta, disabled):
        btn = discord.ui.Button(label=label, row=0, style=discord.ButtonStyle.secondary, disabled=disabled)

        async def cb(interaction: discord.Interaction):
            embed, new_page, _ = build_normal_leaderboard(self.stat, self.page + delta)
            await interaction.response.edit_message(embed=embed, view=LeaderboardView(self.stat, new_page))

        btn.callback = cb
        return btn


# ── Big board (admin display): 1-50 in 5 columns, persistent, shared state ───────
BIGBOARD_SIZE = 51       # divisible by 3 columns of 17
BIGBOARD_COLUMNS = 3


def _bigboard_pages(stat_key: str) -> int:
    total = len(_ranked_for_stat(stat_key))
    return max(1, (total + BIGBOARD_SIZE - 1) // BIGBOARD_SIZE)


def build_bigboard_embed() -> discord.Embed:
    cfg = pug_data["config"]
    stat = cfg.get("bigboard_stat", "elo")
    if stat not in STATS:
        stat = "elo"
    pages = _bigboard_pages(stat)
    page = max(0, min(cfg.get("bigboard_page", 0), pages - 1))
    embed, total = build_stat_leaderboard(stat, start=page*BIGBOARD_SIZE, count=BIGBOARD_SIZE, columns=BIGBOARD_COLUMNS)
    lo = page*BIGBOARD_SIZE + 1
    hi = min(total, (page+1)*BIGBOARD_SIZE)
    embed.set_footer(text=f"Ranks {lo}-{hi} of {total} | Page {page+1}/{pages}")
    return embed


async def refresh_bigboard(bot):
    """Re-render the standing big-board message if one has been posted."""
    cfg = pug_data["config"]
    ch = bot.get_channel(cfg.get("bigboard_channel_id"))
    if not ch:
        return
    mid = cfg.get("bigboard_message_id")
    if not mid:
        return
    try:
        msg = await ch.fetch_message(mid)
        await msg.edit(embed=build_bigboard_embed(), view=BigBoardView())
    except discord.NotFound:
        pass


class _BigCycleButton(discord.ui.Button):
    """Persistent single stat-cycle button for the big board (state lives in config)."""

    def __init__(self):
        cur = pug_data["config"].get("bigboard_stat", "elo")
        if cur not in STATS:
            cur = "elo"
        super().__init__(label=f"Stat: {SHORT_LABELS[cur]}", style=discord.ButtonStyle.primary,
                         custom_id="bb_cycle", row=0)

    async def callback(self, interaction: discord.Interaction):
        cur = pug_data["config"].get("bigboard_stat", "elo")
        if cur not in STATS:
            cur = "elo"
        pug_data["config"]["bigboard_stat"] = _next_stat(cur)
        pug_data["config"]["bigboard_page"] = 0
        save_pug_data()
        await interaction.response.edit_message(embed=build_bigboard_embed(), view=BigBoardView())


class _BigPageButton(discord.ui.Button):
    def __init__(self, label, custom_id, delta):
        super().__init__(label=label, row=0, style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        cfg = pug_data["config"]
        stat = cfg.get("bigboard_stat", "elo")
        new_page = cfg.get("bigboard_page", 0) + self.delta
        cfg["bigboard_page"] = max(0, min(new_page, _bigboard_pages(stat) - 1))
        save_pug_data()
        await interaction.response.edit_message(embed=build_bigboard_embed(), view=BigBoardView())


class BigBoardView(discord.ui.View):
    """Persistent big leaderboard: one stat-cycle button + 50-at-a-time paging. State is
    shared (stored in config), so the whole channel sees the same view."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_BigCycleButton())
        self.add_item(_BigPageButton("◀ Prev 50", "bigboard_prev", -1))
        self.add_item(_BigPageButton("Next 50 ▶", "bigboard_next", +1))
