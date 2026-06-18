import io

from PIL import Image, ImageDraw

from scoreboard import make_font

# Dark card matching the rest of the brand visuals.
BG       = (24, 25, 28)
GRID     = (55, 57, 64)
LINE     = (241, 196, 15)   # gold
POINT    = (255, 255, 255)
TEXT     = (180, 180, 195)
UP       = (80, 220, 80)
DOWN     = (220, 80, 80)


def draw_elo_graph(elo_history: list, start_elo: int = 1000) -> io.BytesIO:
    """Render a simple ELO-over-time line graph as a PNG.

    `elo_history` is the list of post-match ELO values (oldest first). A leading
    `start_elo` point is prepended so the very first match still shows a slope.
    """
    W, H = 820, 320
    M_L, M_R, M_T, M_B = 60, 24, 26, 40  # margins
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

    # Title + current value.
    draw.text((M_L, 4), "ELO history", font=f_title, fill=TEXT)
    cur = series[-1]
    cur_txt = f"Current: {cur}"
    bbox = draw.textbbox((0, 0), cur_txt, font=f_title)
    draw.text((W - M_R - (bbox[2] - bbox[0]), 4), cur_txt, font=f_title, fill=LINE)

    # X axis label.
    games_played = max(0, len(series) - 1)
    draw.text((M_L, H - M_B + 14), f"{games_played} games", font=f_lbl, fill=TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
