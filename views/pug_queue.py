from datetime import datetime, timezone

import discord

from core.guild_views import GuildView
from core.guild_ctx import current_guild_or_none
from pug.config import QUEUE_SIZES, BRAND
from pug.storage import (
    pug_data,
    queues_for,
    queue_for,
    queued_sizes,
    leave_all_queues,
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
    """The persistent embed in #queue: one section per size.

    A player can be in several queues at once, so the sections deliberately overlap and
    the same name may appear more than once.
    """
    table = queues_for()
    ready = [key for key, needed in QUEUE_SIZES.items() if len(table[key]) >= needed]

    embed = discord.Embed(
        title="Competitive Krunker League Queue",
        color=0x3FB950 if ready else 0x5865F2,
    )

    for key, needed in QUEUE_SIZES.items():
        queued = table[key]
        if queued:
            lines = []
            for i, pid in enumerate(queued, start=1):
                names = ", ".join(get_player(pid)["usernames"]) or "-"
                lines.append(f"`{i}.` <@{pid}> | {names}")
            value = "\n".join(lines)
        else:
            value = "*Empty*"
        embed.add_field(name=f"{key} Queue \u2014 {len(queued)}/{needed}", value=value, inline=False)

    embed.set_footer(text="Join as many sizes as you like. Click the same button again to leave.")
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


def _join_blocked(interaction: discord.Interaction) -> str | None:
    """Why this user may not queue, or None if they may. Same rules for every size."""
    uid = interaction.user.id

    from pug.config import member_is_viewer
    if member_is_viewer(interaction.user):
        return "Viewers can't join the queue."

    if is_noadded(uid):
        info = get_noadd_info(uid) or {}
        until = info.get("until")
        reason = info.get("reason") or "*No reason provided.*"
        if until:
            expiry = f"Your no-add expires <t:{int(until)}:R> (<t:{int(until)}:f>)."
        else:
            expiry = "Your no-add is **permanent** until an admin removes it."
        return (
            f"You are currently **noadded** and can't queue.\n"
            f"{expiry}\n"
            f"**Reason:** {reason}"
        )

    if not get_player(uid)["usernames"]:
        return (
            "You need a **linked Krunker account** before you can queue.\n"
            f"Post your username + proof in {_account_link_mention()} to get access to the queue."
        )

    from pug.storage import in_active_match
    if in_active_match(uid):
        return "You're already in an active match."

    return None


class _JoinButton(discord.ui.Button):
    """Join one size, or leave it if already in it.

    Toggling rather than a per-size leave button keeps the row inside Discord's limit of
    five while still letting someone back out of a single queue.
    """

    def __init__(self, size: str):
        super().__init__(
            label=f"Join {size}",
            style=discord.ButtonStyle.success,
            custom_id=f"pug_join_{size}",
            row=0,
        )
        self.size = size

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        queue = queue_for(self.size)

        # Already in it -> this is a leave.
        if uid in queue:
            queue.remove(uid)
            still = queued_sizes(uid)
            extra = f" Still queued for {', '.join(still)}." if still else ""
            await interaction.response.send_message(
                f"You left the **{self.size}** queue.{extra}", ephemeral=True
            )
            await interaction.message.edit(embed=build_queue_embed(), view=QueueView())
            return

        blocked = _join_blocked(interaction)
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return

        get_player(uid)  # ensure a record exists
        queue.append(uid)
        needed = QUEUE_SIZES[self.size]

        if len(queue) >= needed:
            # pop_queue takes the players and refreshes the embed.
            from pug.match import pop_queue
            await interaction.response.send_message(
                f"You joined the **{self.size}** queue.", ephemeral=True
            )
            await pop_queue(interaction.guild, interaction.client, size=self.size)
            return

        also = [k for k in queued_sizes(uid) if k != self.size]
        extra = f" Also queued for {', '.join(also)}." if also else ""
        await interaction.response.send_message(
            f"You joined the **{self.size}** queue ({len(queue)}/{needed}).{extra}",
            ephemeral=True,
        )
        await interaction.message.edit(embed=build_queue_embed(), view=QueueView())


class _LeaveAllButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Leave All",
            style=discord.ButtonStyle.secondary,
            custom_id="pug_leave",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        left = leave_all_queues(interaction.user.id)
        if not left:
            await interaction.response.send_message("You're not in any queue.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"You left: **{', '.join(left)}**.", ephemeral=True
        )
        await interaction.message.edit(embed=build_queue_embed(), view=QueueView())


class _LeaderboardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Leaderboard",
            style=discord.ButtonStyle.primary,
            custom_id="pug_leaderboard",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        embed, _, _ = build_normal_leaderboard("elo", 0)
        await interaction.response.send_message(
            embed=embed, view=LeaderboardView("elo", 0), ephemeral=True
        )


class QueueView(GuildView):
    """One Join button per size, plus Leave All and the leaderboard.

    Built with add_item rather than decorators so the sizes come from QUEUE_SIZES and
    the button order matches the embed.
    """

    def __init__(self):
        super().__init__(timeout=None)
        for size in QUEUE_SIZES:
            self.add_item(_JoinButton(size))
        self.add_item(_LeaveAllButton())
        self.add_item(_LeaderboardButton())


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


def _mvps(p):
    return p.get("mvps", 0)


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
    "mvp":    ("MVPs",         _mvps,
               lambda p: f"**{_mvps(p)}** MVP" + ("s" if _mvps(p) != 1 else ""),
               lambda p: f"**{_mvps(p)}**", _played_ranked),
}
STAT_ORDER = ["elo", "wl", "games", "rating", "kd", "obj", "mvp"]


def _ranked_for_stat(stat_key: str) -> list:
    _label, value_fn, _full, _short, eligible = STATS[stat_key]
    out = [(int(did), p) for did, p in pug_data["players"].items() if eligible(p)]
    out.sort(key=lambda t: value_fn(t[1]), reverse=True)
    return out


def build_stat_leaderboard(stat_key="elo", start=0, count=10):
    """Build a single-column leaderboard embed for one stat. Returns (embed, total).
    Used by the ephemeral /leaderboard; the big board renders its own table."""
    if stat_key not in STATS:
        stat_key = "elo"
    label, _v, full_disp, _short, _e = STATS[stat_key]
    ranked = _ranked_for_stat(stat_key)
    total = len(ranked)
    chunk = ranked[start:start + count]

    embed = discord.Embed(title=f"{BRAND} Leaderboard: {label}", color=0xF1C40F)
    if not chunk:
        embed.description = "*No ranked players yet. Play a game to get on the board.*"
        embed.set_footer(text="Updated")
        embed.timestamp = datetime.now(timezone.utc)
        return embed, total

    lines = [f"`{start+i+1}.` <@{did}> | {full_disp(p)}" for i, (did, p) in enumerate(chunk)]
    embed.description = "\n".join(lines)
    return embed, total


SHORT_LABELS = {"elo": "ELO", "wl": "W/L", "games": "Games", "rating": "Rating",
                "kd": "K/D", "obj": "OBJ", "mvp": "MVPs"}
NORMAL_PER_PAGE = 10


# ── Normal leaderboard (anyone): top 10 per page, compact stat buttons + paging ──
def build_normal_leaderboard(stat="elo", page=0):
    """Returns (embed, clamped_page, total_pages) for the 10-per-page leaderboard."""
    if stat not in STATS:
        stat = "elo"
    total = len(_ranked_for_stat(stat))
    pages = max(1, (total + NORMAL_PER_PAGE - 1) // NORMAL_PER_PAGE)
    page = max(0, min(page, pages - 1))
    embed, _ = build_stat_leaderboard(stat, page * NORMAL_PER_PAGE, NORMAL_PER_PAGE)
    embed.set_footer(text=f"Page {page+1}/{pages} | {total} ranked players")
    return embed, page, pages


def _next_stat(stat):
    return STAT_ORDER[(STAT_ORDER.index(stat) + 1) % len(STAT_ORDER)]


class LeaderboardView(GuildView):
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


# ── Big board (admin display): persistent multi-stat table, shared state ───────
# Rendered as an ANSI code block so every column lines up. Discord mentions are
# proportional-width pills and can never align, so players are shown by their
# linked Krunker username instead (that is also the name they play under).
# 20 rows, not 25: with every stat column the ANSI colour codes push each row to
# ~170 chars, and 25 rows would overrun Discord's 4096-char embed description.
BIGBOARD_SIZE = 20
BIGBOARD_NAME_W = 12     # name column; full row lands at 55 chars
BIGBOARD_DESC_LIMIT = 4096

# Discord ANSI code-block colours (30-37 fg). Only these are supported.
# Discord ANSI code blocks: foreground 30-37, background 40-47. 40 (#4f545c) is
# the dark grey used to band whichever column the board is currently sorted by.
_FG = {"grey": 30, "red": 31, "green": 32, "gold": 33,
       "blue": 34, "pink": 35, "cyan": 36, "white": 37}
_HL_BG = 40
_RESET = "\u001b[0m"


def _c(fg: str = "white", bold: bool = False, hl: bool = False) -> str:
    """One ANSI escape. Discord's parser expects {style};{background};{foreground}."""
    parts = ["1" if bold else "0"]
    if hl:
        parts.append(str(_HL_BG))
    parts.append(str(_FG.get(fg, 37)))
    return "\u001b[" + ";".join(parts) + "m"


# key -> (header, width, align). Header and data rows are built from this one spec
# so the highlight band lines up exactly between them.
_COLS = [
    ("rank", "#", 2, ">"),
    ("delta", "\u0394", 2, "<"),
    ("name", "PLAYER", BIGBOARD_NAME_W, "<"),
    ("elo", "ELO", 4, ">"),
    ("wl", "W-L", 6, ">"),
    ("kd", "K/D", 5, ">"),
    ("win", "WIN", 4, ">"),
    ("ckl", "CKL", 4, ">"),
    ("obj", "OBJ", 5, ">"),
    ("mvp", "MVP", 3, ">"),
]

# Which displayed column each sort stat bands. "games" has no column of its own,
# so it bands W-L, which carries the same information (wins + losses).
_SORT_COLUMN = {"elo": "elo", "wl": "win", "games": "wl", "rating": "ckl",
                "kd": "kd", "obj": "obj", "mvp": "mvp"}


def _join_cells(cells, hl_key) -> str:
    """cells = [(key, text, fg, bold)]. The separator space in front of a highlighted
    column is pulled inside the highlight so the grey band reads as one block."""
    out = []
    for i, (key, text, fg, bold) in enumerate(cells):
        sep = "" if i == 0 else " "
        if key == hl_key:
            out.append(_c(fg, bold, hl=True) + sep + text + _RESET)
        else:
            out.append(sep + _c(fg, bold) + text + _RESET)
    return "".join(out)


def _bigboard_header(stat: str) -> str:
    cells = []
    for key, title, w, align in _COLS:
        if key == "delta" and stat != "elo":
            title = ""      # movement is only tracked for the ELO ordering
        cells.append((key, f"{title:{align}{w}}", "cyan", True))
    return _join_cells(cells, _SORT_COLUMN.get(stat))


def _bigboard_pages(stat_key: str) -> int:
    total = len(_ranked_for_stat(stat_key))
    return max(1, (total + BIGBOARD_SIZE - 1) // BIGBOARD_SIZE)


def _display_name(did: int, p: dict) -> str:
    """Krunker username for the board. Falls back to the discord id if unlinked."""
    names = p.get("usernames") or []
    name = names[0] if names else f"id:{did}"
    return (name[:BIGBOARD_NAME_W - 1] + "…") if len(name) > BIGBOARD_NAME_W else name


def _movement(did: int, stat: str) -> tuple[str, str]:
    """(arrow, colour) for rank movement caused by the most recent match. Reads the
    delta computed at snapshot time rather than recomputing, so the arrows stay put
    while people page/sort through the board. ELO-only: it is the only ranking whose
    ordering we persist."""
    if stat != "elo":
        return "", "grey"
    cfg = pug_data["config"]
    if str(did) not in cfg.get("bigboard_prev_ranks", {}):
        return "·", "grey"          # new to the board
    delta = cfg.get("bigboard_deltas", {}).get(str(did), 0)
    if delta == 0:
        return "-", "grey"
    arrow = "▲" if delta > 0 else "▼"
    mag = abs(delta)
    return (f"{arrow}{mag}" if mag < 10 else f"{arrow}+"), ("green" if delta > 0 else "red")


def snapshot_bigboard_ranks() -> None:
    """Recompute movement against the previous ELO ordering, then store the new one.
    Called only after a match finishes. If this ran on every render, the deltas would
    reset to '-' the moment anyone clicked a page button."""
    cfg = pug_data["config"]
    prev = cfg.get("bigboard_prev_ranks", {})
    new = {str(did): i + 1 for i, (did, _p) in enumerate(_ranked_for_stat("elo"))}
    cfg["bigboard_deltas"] = {
        did: prev[did] - rank for did, rank in new.items()
        if did in prev and prev[did] != rank
    }
    cfg["bigboard_prev_ranks"] = new


def _bigboard_row(rank: int, did: int, p: dict, stat: str) -> str:
    name = _display_name(did, p)
    wins, losses = p.get("wins", 0), p.get("losses", 0)
    wl = f"{wins}-{losses}"
    wl = wl if len(wl) <= 6 else wl[:6]
    wr = round(_winrate(p) * 100)
    kd = _kd(p)
    # _kd() returns float(kills) on a zero-death game, so clamp the display or a
    # single flawless round would widen the column and break the alignment.
    kd_txt = f"{kd:.2f}" if kd < 100 else "99+"
    rating = _avg_rating(p)
    obj = round(_avg_obj(p))
    obj_txt = str(obj) if obj < 10000 else "9999+"
    mvps = p.get("mvps", 0)

    rank_col = {1: "gold", 2: "white", 3: "red"}.get(rank, "grey")
    name_col = rank_col if rank <= 3 else "white"
    kd_col = "green" if kd >= 1.3 else ("red" if kd < 0.9 else "white")
    wr_col = "green" if wr >= 55 else ("red" if wr < 45 else "white")
    rating_col = "green" if rating >= 6.5 else ("red" if 0 < rating < 4.5 else "white")
    mvp_col = "gold" if mvps else "grey"
    arrow, arrow_col = _movement(did, stat)

    cells = [
        ("rank", f"{rank:>2}", rank_col, rank <= 3),
        ("delta", f"{arrow:<2}", arrow_col, False),
        ("name", f"{name:<{BIGBOARD_NAME_W}}", name_col, rank <= 3),
        ("elo", f"{p.get('elo', 0):>4}", "gold", True),
        ("wl", f"{wl:>6}", "white", False),
        ("kd", f"{kd_txt:>5}", kd_col, False),
        ("win", f"{str(wr) + '%':>4}", wr_col, False),
        ("ckl", f"{rating:>4.1f}", rating_col, False),
        ("obj", f"{obj_txt:>5}", "cyan", False),
        ("mvp", f"{mvps:>3}", mvp_col, False),
    ]
    return _join_cells(cells, _SORT_COLUMN.get(stat))


def build_bigboard_embed() -> discord.Embed:
    cfg = pug_data["config"]
    stat = cfg.get("bigboard_stat", "elo")
    if stat not in STATS:
        stat = "elo"
    ranked = _ranked_for_stat(stat)
    total = len(ranked)
    pages = _bigboard_pages(stat)
    page = max(0, min(cfg.get("bigboard_page", 0), pages - 1))
    chunk = ranked[page * BIGBOARD_SIZE:(page + 1) * BIGBOARD_SIZE]

    embed = discord.Embed(title=f"{BRAND} Leaderboard", color=0xF1C40F)
    if not chunk:
        embed.description = "*No ranked players yet. Play a game to get on the board.*"
        embed.set_footer(text="Updated")
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    header = _bigboard_header(stat)
    rows = [_bigboard_row(page * BIGBOARD_SIZE + i + 1, did, p, stat)
            for i, (did, p) in enumerate(chunk)]
    # Never let a wide row set overrun the embed description: drop rows off the end
    # until it fits rather than letting Discord reject the whole edit.
    def _wrap(rs):
        return "```ansi\n" + header + "\n" + "\n".join(rs) + "\n```"

    while rows and len(_wrap(rows)) > BIGBOARD_DESC_LIMIT - 120:
        rows.pop()
    block = _wrap(rows)

    embed.description = block
    lo = page * BIGBOARD_SIZE + 1
    hi = min(total, (page + 1) * BIGBOARD_SIZE)
    # Footer text is plain -- Discord does not render <t:...> markdown there -- so the
    # update time goes in as a real embed timestamp, which the client formats itself.
    embed.set_footer(text=f"Ranks {lo}-{hi} of {total}  |  Page {page+1}/{pages}  |  Updated")
    embed.timestamp = datetime.now(timezone.utc)
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
    snapshot_bigboard_ranks()   # recompute movement before rendering
    save_pug_data()
    try:
        msg = await ch.fetch_message(mid)
        await msg.edit(embed=build_bigboard_embed(), view=BigBoardView())
    except discord.NotFound:
        pass


class _BigCycleButton(discord.ui.Button):
    """Persistent single stat-cycle button for the big board (state lives in config)."""

    def __init__(self):
        # Built once (no guild context) when the persistent view is registered at startup,
        # and again per-guild when the board is posted/refreshed. The startup label is just
        # a template -- only the custom_id matters for routing -- so fall back to "elo".
        cur = "elo"
        if current_guild_or_none() is not None:
            cur = pug_data["config"].get("bigboard_stat", "elo")
        if cur not in STATS:
            cur = "elo"
        super().__init__(label=f"Sort: {SHORT_LABELS[cur]}", style=discord.ButtonStyle.primary,
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


class BigBoardView(GuildView):
    """Persistent big leaderboard: one stat-cycle button + 50-at-a-time paging. State is
    shared (stored in config), so the whole channel sees the same view."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(_BigCycleButton())
        self.add_item(_BigPageButton(f"◀ Prev {BIGBOARD_SIZE}", "bigboard_prev", -1))
        self.add_item(_BigPageButton(f"Next {BIGBOARD_SIZE} ▶", "bigboard_next", +1))
