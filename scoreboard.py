from PIL import Image, ImageDraw, ImageFont
import io
import os

KRUNKER_FONT  = os.path.join(os.path.dirname(__file__), "krunker_font.ttf")
FALLBACK_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def make_font(size):
    try:
        return ImageFont.truetype(KRUNKER_FONT, size)
    except:
        try:
            return ImageFont.truetype(FALLBACK_BOLD, size)
        except:
            return ImageFont.load_default()

def make_label_font(size):
    for path in ("C:/Windows/Fonts/arialbd.ttf", FALLBACK_BOLD):
        try:
            return ImageFont.truetype(path, size)
        except:
            pass
    return make_font(size)

def _clamp(v, lo=0.0, hi=10.0):
    return max(lo, min(hi, v))


# Maps the raw weighted average -> final score. Lifts the 7-9 band so good games feel
# rewarding, but the last stretch (9.6 -> 10) stays steep so 9.7-10 is reserved for
# near-flawless per-round lines.
_RATING_ANCHORS = [(0, 0), (3, 2), (5, 5), (6.5, 7), (8, 8.5), (9, 9.2), (9.85, 9.6), (10, 10)]


def _rating_curve(x: float) -> float:
    a = _RATING_ANCHORS
    if x <= a[0][0]:
        return a[0][1]
    if x >= a[-1][0]:
        return a[-1][1]
    for i in range(len(a) - 1):
        x0, y0 = a[i]
        x1, y1 = a[i + 1]
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return x


def ckl_rating(kills: int, deaths: int, obj: int, dmg: int, rounds: int = 2) -> float:
    """CKL performance rating on a 0-10 scale.

    Calibrated to a 2-round baseline so 2-1 maps do not automatically inflate CKL
    rating from having more time to collect kills, OBJ, and damage.

      ~9-10 godlike | ~8 elite | ~7 great | ~5 average | ~3-4 bad | ~1 awful
    """
    kills = max(0, int(kills or 0))
    deaths = max(0, int(deaths or 0))
    obj = max(0, int(obj or 0))
    dmg = max(0, int(dmg or 0))
    rounds = max(1, int(rounds or 2))

    if kills == 0 and deaths == 0 and obj == 0 and dmg == 0:
        return 0.0

    round_factor = 2.0 / rounds
    norm_kills = kills * round_factor
    norm_deaths = deaths * round_factor
    norm_obj = obj * round_factor
    norm_dmg = dmg * round_factor

    kd = kills / deaths if deaths else float(kills)
    kd_s = 5 + (kd - 1) * 5
    kill_s = norm_kills / 5
    surv_s = max(0, (60 - norm_deaths) * 0.25)
    obj_s = norm_obj / 130
    dmg_s = norm_dmg / 700

    avg = (
        kd_s * 0.15 +
        kill_s * 0.25 +
        surv_s * 0.10 +
        obj_s * 0.20 +
        dmg_s * 0.30
    )
    return round(_clamp(avg), 1)


def _lerp(a, b, f):
    return tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))


def rating_color(r: float) -> tuple:
    """Map a 0-10 rating to red -> yellow -> green."""
    t = max(0.0, min(1.0, r / 10.0))
    if t < 0.5:
        return _lerp((220, 60, 60), (241, 196, 15), t / 0.5)
    return _lerp((241, 196, 15), (90, 210, 90), (t - 0.5) / 0.5)


def _draw_team_bar_h(draw, cx, cy, width, height, label, a, b, color_a, color_b, f_label, f_val):
    """A horizontal team-total bar (Krunker style): team A fills from the left, team B from
    the right, proportional to a:b. Each team's total sits at its end in its colour, and the
    stat label sits in a dark pill at the centre."""
    a = max(0, int(a or 0))
    b = max(0, int(b or 0))
    total = a + b
    frac = (a / total) if total else 0.5
    x0, x1 = cx - width / 2, cx + width / 2
    y0, y1 = cy - height / 2, cy + height / 2
    split = x0 + width * frac
    draw.rectangle([x0, y0, split, y1], fill=color_a)
    draw.rectangle([split, y0, x1, y1], fill=color_b)
    draw.rectangle([x0, y0, x1, y1], outline=(15, 16, 19), width=2)
    # Faint 50% reference mark -- the pill's offset from it shows who's ahead.
    draw.line([(cx, y0 - 4), (cx, y1 + 4)], fill=(230, 230, 240, 120), width=1)

    def _vtext(x, s, font, fill, anchor_right=False):
        bb = draw.textbbox((0, 0), s, font=font)
        tw = bb[2] - bb[0]
        ty = cy - (bb[3] + bb[1]) / 2
        draw.text(((x - tw) if anchor_right else x, ty), s, font=font, fill=fill)

    _vtext(x0 - 12, f"{a:,}", f_val, color_a, anchor_right=True)
    _vtext(x1 + 12, f"{b:,}", f_val, color_b)

    # The label pill rides the split (clamped inside the bar), so where it lands tells you
    # at a glance which team's fill is longer.
    lbb = draw.textbbox((0, 0), label, font=f_label)
    lw = lbb[2] - lbb[0]
    pad = 6
    pcx = min(max(split, x0 + lw / 2 + pad + 2), x1 - lw / 2 - pad - 2)
    ph = height / 2 + 3
    draw.rectangle([pcx - lw / 2 - pad, cy - ph, pcx + lw / 2 + pad, cy + ph],
                   fill=(18, 19, 22), outline=(90, 92, 100), width=1)
    _vtext(pcx - lw / 2, label, f_label, WHITE)


def _draw_check(draw, x, y, s=18, color=(58, 160, 255)):
    """A small Krunker-style verified badge: a blue disc with a white check, top-left at (x, y)."""
    draw.ellipse([x, y, x + s, y + s], fill=color)
    draw.line(
        [(x + s * 0.27, y + s * 0.52), (x + s * 0.43, y + s * 0.70), (x + s * 0.76, y + s * 0.30)],
        fill=(255, 255, 255), width=2, joint="curve",
    )


def _draw_trophy(draw, x, y, s=26, color=(241, 196, 15)):
    """A small gold trophy icon with its top-left at (x, y)."""
    cx = x + s / 2
    top = y + s * 0.08
    bowl_w = s * 0.52
    draw.polygon([
        (cx - bowl_w / 2, top),
        (cx + bowl_w / 2, top),
        (cx + bowl_w * 0.30, top + s * 0.42),
        (cx - bowl_w * 0.30, top + s * 0.42),
    ], fill=color)
    hw = s * 0.18
    draw.arc([cx - bowl_w / 2 - hw, top - 1, cx - bowl_w / 2 + hw * 0.4, top + s * 0.30], 70, 290, fill=color, width=2)
    draw.arc([cx + bowl_w / 2 - hw * 0.4, top - 1, cx + bowl_w / 2 + hw, top + s * 0.30], 250, 110, fill=color, width=2)
    draw.rectangle([cx - s * 0.05, top + s * 0.42, cx + s * 0.05, top + s * 0.60], fill=color)
    draw.rectangle([cx - s * 0.20, top + s * 0.60, cx + s * 0.20, top + s * 0.68], fill=color)
    draw.rectangle([cx - s * 0.30, top + s * 0.68, cx + s * 0.30, top + s * 0.78], fill=color)


def _draw_curved_mvp(img, x, y, font, color):
    placements = [
        # letter, x position, y position, angle
        ("M", x- 23, y - 13, 30),
        ("V", x - 5, y - 16, -0),
        ("P", x + 3,  y - 13,  -30),
    ]

    for ch, px, py, angle in placements:
        bb = font.getbbox(ch)
        layer = Image.new("RGBA", (bb[2] - bb[0] + 8, bb[3] - bb[1] + 8), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.text((4 - bb[0], 4 - bb[1]), ch, font=font, fill=color)

        layer = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(layer, (int(px), int(py)))

def ckl_logo(size: int) -> "Image.Image":
    """A clean, rounded-square CKL badge. Uses ckl_logo.png from the project root if
    present, otherwise draws a white badge with padded black 'CKL'. Rendered at 4x and
    downscaled for smooth edges."""
    asset = os.path.join(os.path.dirname(__file__), "ckl_logo.png")
    if os.path.exists(asset):
        try:
            base = Image.open(asset).convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.24), fill=255)
            base.putalpha(mask)
            return base
        except Exception:
            pass

    # Supersample for crisp, anti-aliased edges and text.
    S = size * 4
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    rad = int(S * 0.24)
    pad = max(2, S // 28)
    # White rounded plate with a dark outline.
    d.rounded_rectangle([pad, pad, S - pad - 1, S - pad - 1], radius=rad,
                        fill=(247, 247, 249, 255), outline=(22, 23, 27), width=max(3, S // 26))
    # Padded, centered "CKL".
    f = make_font(int(S * 0.30))
    bb = d.textbbox((0, 0), "CKL", font=f)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((S - tw) / 2 - bb[0], (S - th) / 2 - bb[1]), "CKL", font=f, fill=(22, 23, 27))
    return base.resize((size, size), Image.LANCZOS)


RED      = (220, 50, 50)
WHITE    = (255, 255, 255)
GOLD     = (241, 196, 15)
GRAY     = (160, 160, 175)
GREEN    = (80, 220, 80)
STAT_RED = (220, 60, 60)

# Verified clans -- their clan tag renders GOLD on the scoreboard (every other clan is
# grey). TO ADD OR REMOVE A VERIFIED CLAN, just edit this list. Capitalization doesn't
# matter (matching is case-insensitive); keep one clan per line for easy diffs.
VERIFIED_CLAN_NAMES = [
    "Arae",
    "SCTE",
    "Lain",
    "Fame",
    "KPD",
    "Jump",
    "Art",
    "Dev",
]
VERIFIED_CLANS = {c.strip().lower() for c in VERIFIED_CLAN_NAMES}
CYAN     = (88, 220, 245)
PINK     = (255, 105, 135)

W       = 1380
PADDING = 36
ROW_H   = 50
HDR_H   = 36
MAX_ROWS = 10

# Columns shifted left to make room for the CKL Rating column on the right.
C_NUM   = PADDING
C_NAME  = PADDING + 55
C_SCORE = 515
C_KILLS = 635
C_DEATH = 730
C_OBJ   = 825
C_DMG   = 935
RATE_CX = 1095        # center of the CKL Rating column (values + logo header)
ELO_CX  = 1215
BONUS_CX = 1320
LOGO_SIZE = 40

MAPS_DIR        = os.path.join(os.path.dirname(__file__), "maps")
BG_IMAGE_PATH   = os.path.join(os.path.dirname(__file__), "scoreboard_bg.png")

def get_bg_for_map(map_name: str) -> str:
    """Return the map-specific background if it exists, otherwise the default."""
    map_bg = os.path.join(MAPS_DIR, f"{map_name.lower()}.png")
    if os.path.exists(map_bg):
        return map_bg
    return BG_IMAGE_PATH

ROW1_H = 70
ROW2_H = 38
TOP_H  = 16 + ROW1_H + ROW2_H + 16 + PADDING * 2
FOOTER_H = 66   # team OBJ/DMG bars under the leaderboard (replaces the old bottom margin)


def _render_scoreboard(
    tournament_name: str,
    map_name: str,
    team1_name: str,
    team1_score: int,
    team1_players: list,
    team1_color: tuple,
    team2_name: str,
    team2_score: int,
    team2_players: list,
    team2_color: tuple,
    show_elo: bool = False,
    clans: dict | None = None,
    verified: set | None = None,
):
    """Render the scoreboard and return (PIL image, rows) where rows is a list of
    {name, y, color} describing each drawn player row (y = row top).

    `clans` is an optional {username_lower: clan_tag} map; a player's clan is shown in
    grey brackets after their name. `verified` is a set of lowercased usernames that get
    a blue verified checkmark."""
    rows = []
    rounds = max(1, int(team1_score or 0) + int(team2_score or 0))  # BO3: 2-0 -> 2, 2-1 -> 3

    all_players = (
        [(p, team1_color) for p in team1_players] +
        [(p, team2_color) for p in team2_players]
    )
    all_players.sort(key=lambda x: x[0].get("score", 0), reverse=True)
    all_players = all_players[:MAX_ROWS]
    show_elo = show_elo or any(
        "elo_base" in p or "elo_delta" in p or "elo_bonus" in p or "elo_change" in p
        for p, _ in all_players
    )

    total_h = TOP_H + HDR_H + len(all_players) * ROW_H + FOOTER_H

    max_kills  = max((p.get("kills", 0)           for p, _ in all_players), default=0)
    min_kills  = min((p.get("kills", 0)           for p, _ in all_players), default=0)
    max_deaths = max((p.get("deaths", 0)          for p, _ in all_players), default=0)
    min_deaths = min((p.get("deaths", 0)          for p, _ in all_players), default=0)
    max_obj    = max((p.get("objective_score", 0) for p, _ in all_players), default=0)
    min_obj    = min((p.get("objective_score", 0) for p, _ in all_players), default=0)
    max_dmg    = max((p.get("damage_done", 0)     for p, _ in all_players), default=0)
    min_dmg    = min((p.get("damage_done", 0)     for p, _ in all_players), default=0)

    img = Image.new("RGBA", (W, total_h), (0, 0, 0, 0))

    bg_path = get_bg_for_map(map_name)
    if os.path.exists(bg_path):
        try:
            bg = Image.open(bg_path).convert("RGBA").resize((W, total_h))
            overlay = Image.new("RGBA", (W, total_h), (0, 0, 0, 120))
            bg = Image.alpha_composite(bg, overlay)
            img = Image.alpha_composite(img, bg)
        except Exception:
            pass

    draw = ImageDraw.Draw(img)

    f_tiny   = make_font(13)
    f_small  = make_font(17)
    f_reg    = make_font(20)
    f_bold   = make_font(24)
    f_map    = make_font(54)
    f_teams  = make_font(26)
    f_winner = make_font(26)
    f_col    = make_font(17)

    y = PADDING

    draw.text((PADDING, y), tournament_name, font=f_tiny, fill=GRAY)
    y += 18

    t1_text = f"{team1_name} ({team1_score})"
    bbox_t1 = draw.textbbox((0, 0), t1_text, font=f_teams)
    t1_y = y + (ROW1_H - (bbox_t1[3] - bbox_t1[1])) // 2
    draw.text((PADDING, t1_y), t1_text, font=f_teams, fill=team1_color)
    if team1_score > team2_score:
        _draw_trophy(draw, PADDING + draw.textlength(t1_text, font=f_teams) + 12, t1_y + 2, 28)

    map_text = map_name.upper()
    bbox_map = draw.textbbox((0, 0), map_text, font=f_map)
    draw.text((W - PADDING - (bbox_map[2] - bbox_map[0]), y), map_text, font=f_map, fill=WHITE)
    y += ROW1_H

    t2_text = f"{team2_name} ({team2_score})"
    draw.text((PADDING, y), t2_text, font=f_teams, fill=team2_color)
    if team2_score > team1_score:
        _draw_trophy(draw, PADDING + draw.textlength(t2_text, font=f_teams) + 12, y + 2, 28)

    winner = team1_name if team1_score > team2_score else team2_name
    series = f"{max(team1_score, team2_score)}-{min(team1_score, team2_score)}"
    after  = f" wins the map {series}"
    bbox_w = draw.textbbox((0, 0), winner, font=f_winner)
    bbox_a = draw.textbbox((0, 0), after,  font=f_winner)
    total_w = (bbox_w[2] - bbox_w[0]) + (bbox_a[2] - bbox_a[0])
    wx = W - PADDING - total_w
    draw.text((wx, y), winner, font=f_winner, fill=GOLD)
    wx += bbox_w[2] - bbox_w[0]
    draw.text((wx, y), after, font=f_winner, fill=WHITE)
    y += ROW2_H + 16

    headers = [
        (C_NUM,   "#"),
        (C_NAME,  "Name"),
        (C_SCORE, "Score"),
        (C_KILLS, "Kills"),
        (C_DEATH, "Deaths"),
        (C_OBJ,   "Obj"),
        (C_DMG,   "Dmg"),
    ]
    for cx, label in headers:
        draw.text((cx, y + 8), label, font=f_col, fill=GRAY)
    # CKL Rating column header - plain text, same font/color/baseline as the other headers.
    rate_hdr = "CKL Rating"
    rbb = draw.textbbox((0, 0), rate_hdr, font=f_col)
    draw.text((RATE_CX - (rbb[2] - rbb[0]) // 2, y + 8), rate_hdr, font=f_col, fill=GRAY)
    if show_elo:
        for cx, label in ((ELO_CX, "ELO"), (BONUS_CX, "Bonus")):
            bb = draw.textbbox((0, 0), label, font=f_col)
            draw.text((cx - (bb[2] - bb[0]) // 2, y + 8), label, font=f_col, fill=GRAY)
    draw.line([(PADDING, y + HDR_H - 1), (W - PADDING, y + HDR_H - 1)], fill=(120, 120, 150, 200), width=1)
    y += HDR_H

    for i, (p, color) in enumerate(all_players):
        row_top = y
        text_y = y + (ROW_H - 22) // 2
        name   = p.get("name", "")
        rows.append({"name": name, "y": row_top, "color": color})
        kills  = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        obj    = p.get("objective_score", 0)
        dmg    = int(p.get("damage_done", 0))
        score  = p.get("score", 0)

        mvp = (i == 0)
        draw.text((C_NUM, text_y), f"{i+1}.", font=f_small, fill=GRAY)

        if name:
            def put(x, s, font, fill, bold=False):
                draw.text((x, text_y), s, font=font, fill=fill)
                if bold:
                    draw.text((x + 1, text_y), s, font=font, fill=fill)

            def put_center(cx, s, font, fill, bold=False):
                bb = draw.textbbox((0, 0), s, font=font)
                x = cx - (bb[2] - bb[0]) // 2
                draw.text((x, text_y), s, font=font, fill=fill)
                if bold:
                    draw.text((x + 1, text_y), s, font=font, fill=fill)

            def signed(n):
                n = int(n or 0)
                return f"+{n}" if n > 0 else str(n)

            def high_low_fill(value, low, high, higher_is_better=True):
                if low == high:
                    return WHITE
                if higher_is_better:
                    if value == high:
                        return GREEN
                    if value == low:
                        return STAT_RED
                else:
                    if value == low:
                        return GREEN
                    if value == high:
                        return STAT_RED
                return WHITE

            nlow = name.strip().lower()
            clan = (clans or {}).get(nlow)
            is_verified = nlow in (verified or set())
            # Order: [verified check] name [clan]. The check sits in the rank/name gap so
            # names stay column-aligned whether or not a player is verified.
            if is_verified:
                _draw_check(draw, C_NAME - 31, text_y + 5, 18)
            disp_name = name[:20] if clan else name[:26]
            put(C_NAME,  disp_name, f_bold, GOLD if mvp else color, bold=mvp)
            if clan:
                tag_x = C_NAME + draw.textlength(disp_name, font=f_bold) + 12
                clan_color = GOLD if clan.strip().lower() in VERIFIED_CLANS else GRAY
                draw.text((tag_x, text_y), f"[{clan}]", font=f_bold, fill=clan_color)
            put(C_SCORE, str(score), f_reg, WHITE)
            put(C_KILLS, str(kills), f_reg, high_low_fill(kills, min_kills, max_kills))
            put(C_DEATH, str(deaths), f_reg, high_low_fill(deaths, min_deaths, max_deaths, higher_is_better=False))
            put(C_OBJ,   str(obj), f_reg, high_low_fill(obj, min_obj, max_obj))
            put(C_DMG,   str(dmg), f_reg, high_low_fill(dmg, int(min_dmg), int(max_dmg)))

            rating = ckl_rating(kills, deaths, obj, dmg, rounds)
            rtxt = f"{rating:.1f}"
            rbb = draw.textbbox((0, 0), rtxt, font=f_bold)
            put(RATE_CX - (rbb[2] - rbb[0]) // 2, rtxt, f_bold, rating_color(rating))

            if show_elo:
                has_change = "elo_base" in p or "elo_delta" in p or "elo_bonus" in p or "elo_change" in p
                if not has_change:
                    put_center(ELO_CX, "--", f_reg, GRAY)
                    put_center(BONUS_CX, "--", f_reg, GRAY)
                    draw.line([(PADDING, y + ROW_H - 1), (W - PADDING, y + ROW_H - 1)], fill=(80, 80, 100, 100), width=1)
                    y += ROW_H
                    continue

                change = p.get("elo_change") or {}
                base = int(p.get("elo_base", change.get("base", p.get("elo_delta", change.get("delta", 0)))) or 0)
                bonus = int(p.get("elo_bonus", change.get("bonus", 0)) or 0)
                delta_fill = GREEN if base > 0 else (STAT_RED if base < 0 else GRAY)
                bonus_fill = CYAN if bonus > 0 else (PINK if bonus < 0 else GRAY)
                put_center(ELO_CX, signed(base), f_reg, delta_fill)

                btxt = signed(bonus)
                put_center(BONUS_CX, btxt, f_reg, bonus_fill)

        draw.line([(PADDING, y + ROW_H - 1), (W - PADDING, y + ROW_H - 1)], fill=(80, 80, 100, 100), width=1)
        y += ROW_H

    # Team-total bars under the leaderboard: OBJ then DMG, horizontal (Krunker style).
    t1_obj = sum(int(p.get("objective_score", 0)) for p in team1_players)
    t2_obj = sum(int(p.get("objective_score", 0)) for p in team2_players)
    t1_dmg = sum(int(p.get("damage_done", 0)) for p in team1_players)
    t2_dmg = sum(int(p.get("damage_done", 0)) for p in team2_players)
    f_barlbl = make_font(11)
    f_barval = make_font(15)
    bar_cx, bar_w, bar_h = W // 2, 680, 11
    # Even *visual* spacing: equal gaps above, between, and below the two bars. The value
    # labels are ~16px tall, so we space by their half-height (h), not just the centers.
    h = 8
    g = (FOOTER_H - 4 * h) // 3
    _draw_team_bar_h(draw, bar_cx, y + g + h + 20, bar_w, bar_h, "OBJ", t1_obj, t2_obj, team1_color, team2_color, f_barlbl, f_barval)
    _draw_team_bar_h(draw, bar_cx, y + FOOTER_H - g - h + 20, bar_w, bar_h, "DMG", t1_dmg, t2_dmg, team1_color, team2_color, f_barlbl, f_barval)

    return img, rows


def draw_scoreboard(
    tournament_name: str,
    map_name: str,
    team1_name: str,
    team1_score: int,
    team1_players: list,
    team1_color: tuple,
    team2_name: str,
    team2_score: int,
    team2_players: list,
    team2_color: tuple,
    show_elo: bool = False,
    clans: dict | None = None,
    verified: set | None = None,
) -> io.BytesIO:
    img, _ = _render_scoreboard(
        tournament_name, map_name,
        team1_name, team1_score, team1_players, team1_color,
        team2_name, team2_score, team2_players, team2_color,
        show_elo=show_elo, clans=clans, verified=verified,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# Column pixel ranges used to highlight/magnify a flagged stat.
_FLAG_COLUMNS = {
    "kd":  (C_KILLS - 22, C_OBJ - 24),   # kills + deaths cells
    "obj": (C_OBJ - 22, C_DMG - 24),     # obj cell
}


def draw_flag_scoreboard(
    tournament_name: str,
    map_name: str,
    team1_name: str,
    team1_score: int,
    team1_players: list,
    team1_color: tuple,
    team2_name: str,
    team2_score: int,
    team2_players: list,
    team2_color: tuple,
    highlight_name: str,
    column: str,  # "kd" or "obj"
) -> io.BytesIO:
    """Like draw_scoreboard, but dims everything except the flagged player's row and
    draws a magnified, red-bordered inset over the offending stat cell(s)."""
    img, rows = _render_scoreboard(
        tournament_name, map_name,
        team1_name, team1_score, team1_players, team1_color,
        team2_name, team2_score, team2_players, team2_color,
    )
    img = img.convert("RGBA")

    target = next((r for r in rows if r["name"].strip().lower() == (highlight_name or "").strip().lower()), None)
    if not target:
        # Can't locate the row; just return the plain board.
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    row_top = target["y"]
    row_bottom = row_top + ROW_H

    # 1) Dim the whole board, then paste the flagged row back at full brightness.
    bright_row = img.crop((0, row_top, W, row_bottom)).copy()
    dim = Image.new("RGBA", img.size, (0, 0, 0, 150))
    img = Image.alpha_composite(img, dim)
    img.paste(bright_row, (0, row_top))

    draw = ImageDraw.Draw(img)
    # Subtle highlight bar behind the row.
    hl = Image.new("RGBA", (W - PADDING * 2, ROW_H), (241, 196, 15, 28))
    img.alpha_composite(hl, (PADDING, row_top))

    # 2) Magnified inset over the offending cell(s).
    x1, x2 = _FLAG_COLUMNS.get(column, _FLAG_COLUMNS["kd"])
    pad = 6
    cell = img.crop((x1, row_top + pad, x2, row_bottom - pad))
    scale = 1.6
    big = cell.resize((int(cell.width * scale), int(cell.height * scale)), Image.LANCZOS)

    cx = (x1 + x2) // 2
    cy = (row_top + row_bottom) // 2
    bx = cx - big.width // 2
    by = cy - big.height // 2

    # Backing plate so the magnified text is readable, then the inset, then a red border.
    draw = ImageDraw.Draw(img)
    draw.rectangle([bx - 4, by - 4, bx + big.width + 4, by + big.height + 4], fill=(18, 19, 22, 255))
    img.alpha_composite(big, (bx, by))
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [bx - 4, by - 4, bx + big.width + 4, by + big.height + 4],
        outline=(220, 50, 50), width=3,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


if __name__ == "__main__":
    # Realistic test card. Mix of: verified clans (gold tag: Arae/Fame/KPD), regular clans
    # (grey: TRGT/Hovi/VEIL), verified players (blue check -- incl. a clanless one), and
    # plain players. `clans` is keyed by lowercased name; `verified` is a set of names.
    t1 = [
        {"name": "opp_reality", "score": 7915, "kills": 60, "deaths": 35, "objective_score": 890,  "damage_done": 5955, "elo_base": 84, "elo_bonus": 30},
        {"name": "tjwysk",      "score": 6580, "kills": 49, "deaths": 47, "objective_score": 950,  "damage_done": 5842, "elo_base": 66, "elo_bonus": 12},
        {"name": "Lvpez",       "score": 5640, "kills": 44, "deaths": 45, "objective_score": 810,  "damage_done": 5560, "elo_base": 71, "elo_bonus": 21},
        {"name": "Lunarz",      "score": 5210, "kills": 37, "deaths": 47, "objective_score": 910,  "damage_done": 5087, "elo_base": 49, "elo_bonus": -4},
    ]
    t2 = [
        {"name": "BananaRunnerA", "score": 6340, "kills": 51, "deaths": 41, "objective_score": 770,  "damage_done": 5159, "elo_base": -43, "elo_bonus": 18},
        {"name": "Vxmpleo",       "score": 6060, "kills": 45, "deaths": 54, "objective_score": 740,  "damage_done": 5512, "elo_base": -58, "elo_bonus": 4},
        {"name": "Allphaa",       "score": 5325, "kills": 38, "deaths": 45, "objective_score": 1060, "damage_done": 4726, "elo_base": -67, "elo_bonus": -3},
        {"name": "Beefguy_",      "score": 4615, "kills": 38, "deaths": 41, "objective_score": 630,  "damage_done": 5087, "elo_base": -76, "elo_bonus": -14},
    ]
    clans = {
        "tjwysk": "Arae",        # verified clan -> gold tag, verified player -> check
        "opp_reality": "Fame",   # verified clan -> gold, verified player
        "lvpez": "KPD",          # verified clan -> gold, but player NOT verified (no check)
        "bananarunnera": "TRGT", # regular clan -> grey, verified player
        "vxmpleo": "Hovi",       # regular clan -> grey, not verified
        "allphaa": "VEIL",       # regular clan -> grey, verified player
        # Lunarz + Beefguy_ are clanless
    }
    verified = {"tjwysk", "opp_reality", "bananarunnera", "allphaa", "lunarz"}  # lunarz is clanless+verified
    buf = draw_scoreboard(
        tournament_name="Competitive Krunker League",
        map_name="Undergrowth",
        team1_name="opp_reality",
        team1_score=2,
        team1_players=t1,
        team1_color=RED,
        team2_name="BananaRunnerA",
        team2_score=0,
        team2_players=t2,
        team2_color=WHITE,
        show_elo=True,
        clans=clans,
        verified=verified,
    )
    out_path = os.path.join(os.path.dirname(__file__), "scoreboard_preview.png")
    with open(out_path, "wb") as f:
        f.write(buf.read())
    print(f"Saved {out_path}")
