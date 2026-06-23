import io
from datetime import datetime

from PIL import Image, ImageDraw

from scoreboard import make_font

# Dark card matching the rest of the brand visuals.
BG       = (24, 25, 28)
GRID     = (55, 57, 64)
VGRID    = (38, 40, 46)
LINE     = (241, 196, 15)   # gold
POINT    = (255, 255, 255)
TEXT     = (180, 180, 195)
AXIS     = (140, 142, 150)
DIM      = (124, 126, 135)
UP       = (92, 204, 99)
DOWN     = (226, 85, 79)

# Rank card size (pug/backgrounds.py crops uploads to match this).
CARD_W, CARD_H = 820, 460

# Stats the /rank card can graph: key -> (label, decimals, is_percent).
RANK_STAT_META = {
    "elo":    ("ELO", 0, False),
    "kd":     ("K/D", 2, False),
    "rating": ("CKL Rating", 2, False),
    "obj":    ("Avg OBJ", 0, False),
    "wr":     ("Win Rate", 0, True),
}

# Past this many games we plot one averaged point per DAY instead of per game, so the
# line and the date labels stay readable on a fixed-width image.
PER_GAME_LIMIT = 40


def draw_elo_graph(elo_history: list, start_elo: int = 1000) -> io.BytesIO:
    """Render a simple ELO-over-time line graph as a PNG.

    `elo_history` is the list of post-match ELO values (oldest first). A leading
    `start_elo` point is prepended so the very first match still shows a slope.
    """
    W, H = 820, 340
    # Top/bottom margins are roomy so the title row and the "N games" label never
    # touch the grid lines or the ELO line.
    M_L, M_R, M_T, M_B = 60, 24, 58, 54  # margins
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_lbl = make_font(15)
    f_title = make_font(18)

    # Build the series: prepend the starting ELO so a single game still draws a line.
    series = [start_elo] + list(elo_history) if elo_history else [start_elo]
    lo, hi = min(series), max(series)
    if hi == lo:
        hi = lo + 1  # avoid a flat divide-by-zero range

    plot_w = W - M_L - M_R
    plot_h = H - M_T - M_B

    def x_at(i):
        if len(series) == 1:
            return M_L + plot_w // 2
        return M_L + int(plot_w * i / (len(series) - 1))

    def y_at(v):
        return M_T + int(plot_h * (1 - (v - lo) / (hi - lo)))

    # Horizontal grid lines + ELO axis labels (4 bands).
    for k in range(5):
        val = lo + (hi - lo) * (4 - k) / 4
        gy = M_T + int(plot_h * k / 4)
        draw.line([(M_L, gy), (W - M_R, gy)], fill=GRID, width=1)
        draw.text((8, gy - 8), str(int(round(val))), font=f_lbl, fill=TEXT)

    # The ELO line.
    pts = [(x_at(i), y_at(v)) for i, v in enumerate(series)]
    if len(pts) >= 2:
        draw.line(pts, fill=LINE, width=3, joint="curve")

    # Points, colored by direction vs the previous point.
    for i, (px, py) in enumerate(pts):
        if i == 0:
            col = POINT
        else:
            col = UP if series[i] > series[i - 1] else (DOWN if series[i] < series[i - 1] else POINT)
        r = 4
        draw.ellipse([px - r, py - r, px + r, py + r], fill=col)

    # Title + current value, centered in the top margin band so they clear the grid.
    title_y = (M_T - 22) // 2
    draw.text((M_L, title_y), "ELO history", font=f_title, fill=TEXT)
    cur = series[-1]
    cur_txt = f"Current: {cur}"
    bbox = draw.textbbox((0, 0), cur_txt, font=f_title)
    draw.text((W - M_R - (bbox[2] - bbox[0]), title_y), cur_txt, font=f_title, fill=LINE)

    # X axis label, placed well below the bottom grid line.
    games_played = max(0, len(series) - 1)
    draw.text((M_L, H - M_B + 22), f"{games_played} games", font=f_lbl, fill=TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ── Multi-stat rank card (per-game trend with selectable stat) ───────────────────
def _fmt(v: float, dec: int, pct: bool) -> str:
    if pct:
        return f"{round(v * 100)}%"
    if dec == 0:
        return f"{round(v):,}"
    return f"{v:.{dec}f}"


def _day_label(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"{dt.month}/{dt.day}"


def _aggregate(series: list) -> list:
    """Turn [(ts, value), ...] into plotted [(label, value), ...]. Per game while small;
    once past PER_GAME_LIMIT, one averaged point per calendar day."""
    if not series:
        return []
    if len(series) <= PER_GAME_LIMIT:
        return [(_day_label(ts), v) for ts, v in series]
    # Group by day, preserving order, averaging the values in each day.
    days = []
    bucket_key, bucket_vals, bucket_ts = None, [], None
    for ts, v in series:
        dt = datetime.fromtimestamp(ts)
        key = (dt.year, dt.month, dt.day)
        if key != bucket_key and bucket_vals:
            days.append((_day_label(bucket_ts), sum(bucket_vals) / len(bucket_vals)))
            bucket_vals = []
        bucket_key, bucket_ts = key, ts
        bucket_vals.append(v)
    if bucket_vals:
        days.append((_day_label(bucket_ts), sum(bucket_vals) / len(bucket_vals)))
    return days


def _base_card(bg_bytes: bytes | None) -> Image.Image:
    """The card canvas: the player's background under a dark scrim (so the chart stays
    legible), or a solid dark card when no background is set."""
    if bg_bytes:
        try:
            bg = Image.open(io.BytesIO(bg_bytes)).convert("RGBA").resize((CARD_W, CARD_H), Image.LANCZOS)
            overlay = Image.new("RGBA", (CARD_W, CARD_H), (10, 11, 13, 170))
            return Image.alpha_composite(bg, overlay).convert("RGBA")
        except Exception:
            pass
    return Image.new("RGBA", (CARD_W, CARD_H), (*BG, 255))


def draw_stat_graph(series: list, stat_key: str, *, bg_bytes: bytes | None = None,
                    games_total: int | None = None) -> io.BytesIO:
    """Render the rank trend card for one stat.

    `series` is [(timestamp, value), ...] oldest-first from a player's snapshot history.
    """
    label, dec, pct = RANK_STAT_META.get(stat_key, RANK_STAT_META["elo"])
    img = _base_card(bg_bytes)
    draw = ImageDraw.Draw(img)

    f_label = make_font(22)
    f_cur = make_font(30)
    f_small = make_font(15)
    f_axis = make_font(14)
    f_date = make_font(13)

    points = _aggregate(series)

    # Header band.
    draw.text((24, 16), label.upper(), font=f_label, fill=TEXT)
    if not points:
        draw.text((24, 54), "Not enough games yet -- play a pug to start the trend.",
                  font=f_small, fill=DIM)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        buf.seek(0)
        return buf

    vals = [v for _lbl, v in points]
    cur, peak, low = vals[-1], max(vals), min(vals)
    avg = sum(vals) / len(vals)
    n_games = games_total if games_total is not None else len(series)

    # Current value next to the label; summary stats right-aligned.
    lbl_w = draw.textlength(label.upper(), font=f_label)
    draw.text((24 + lbl_w + 16, 12), _fmt(cur, dec, pct), font=f_cur, fill=LINE)
    summary = (f"PEAK {_fmt(peak, dec, pct)}   AVG {_fmt(avg, dec, pct)}   "
               f"LOW {_fmt(low, dec, pct)}   {n_games} GAMES")
    sw = draw.textlength(summary, font=f_small)
    draw.text((CARD_W - 24 - sw, 24), summary, font=f_small, fill=DIM)

    # Plot geometry.
    M_L, M_R, M_T, M_B = 58, 58, 74, 44
    plot_w = CARD_W - M_L - M_R
    plot_h = CARD_H - M_T - M_B
    lo, hi = low, peak
    if hi == lo:
        hi = lo + (abs(lo) * 0.1 or 1)
    pad = (hi - lo) * 0.18
    lo -= pad
    hi += pad

    n = len(points)

    def x_at(i):
        return M_L + (plot_w // 2 if n == 1 else int(plot_w * i / (n - 1)))

    def y_at(v):
        return M_T + int(plot_h * (1 - (v - lo) / (hi - lo)))

    # Horizontal grid + real value labels (both sides).
    for k in range(5):
        gy = M_T + int(plot_h * k / 4)
        val = hi - (hi - lo) * k / 4
        draw.line([(M_L, gy), (CARD_W - M_R, gy)], fill=GRID, width=1)
        vtxt = _fmt(val, dec, pct)
        draw.text((M_L - 8 - draw.textlength(vtxt, font=f_axis), gy - 8), vtxt, font=f_axis, fill=AXIS)
        draw.text((CARD_W - M_R + 8, gy - 8), vtxt, font=f_axis, fill=AXIS)

    # Vertical tick per point; date labels thinned to whatever fits the width.
    max_labels = max(2, plot_w // 52)
    step = max(1, (n + max_labels - 1) // max_labels)
    for i, (lbl, _v) in enumerate(points):
        px = x_at(i)
        draw.line([(px, M_T), (px, CARD_H - M_B)], fill=VGRID, width=1)
        if i % step == 0 or i == n - 1:
            lw = draw.textlength(lbl, font=f_date)
            draw.text((px - lw / 2, CARD_H - M_B + 8), lbl, font=f_date, fill=DIM)

    # The line.
    pts = [(x_at(i), y_at(v)) for i, v in enumerate(vals)]
    if len(pts) >= 2:
        draw.line(pts, fill=LINE, width=3, joint="curve")

    # Points coloured by direction; mark current.
    for i, (px, py) in enumerate(pts):
        if i == 0:
            col = POINT
        else:
            col = UP if vals[i] > vals[i - 1] else (DOWN if vals[i] < vals[i - 1] else POINT)
        r = 3
        draw.ellipse([px - r, py - r, px + r, py + r], fill=col)
    cx, cy = pts[-1]
    draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=LINE)

    # Peak / low value callouts.
    maxI, minI = vals.index(peak), vals.index(low)
    pk_txt = _fmt(peak, dec, pct)
    draw.text((x_at(maxI) - draw.textlength(pk_txt, font=f_axis) / 2, y_at(peak) - 22),
              pk_txt, font=f_axis, fill=UP)
    lo_txt = _fmt(low, dec, pct)
    draw.text((x_at(minI) - draw.textlength(lo_txt, font=f_axis) / 2, y_at(low) + 8),
              lo_txt, font=f_axis, fill=DOWN)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf
