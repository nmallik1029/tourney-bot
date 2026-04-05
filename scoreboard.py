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

RED      = (220, 50, 50)
WHITE    = (255, 255, 255)
GOLD     = (241, 196, 15)
GRAY     = (160, 160, 175)
GREEN    = (80, 220, 80)
STAT_RED = (220, 60, 60)

W       = 1200
PADDING = 36
ROW_H   = 50
HDR_H   = 36
MAX_ROWS = 10

C_NUM   = PADDING
C_NAME  = PADDING + 55
C_SCORE = 560
C_KILLS = 680
C_DEATH = 780
C_OBJ   = 880
C_DMG   = 1000

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
) -> io.BytesIO:

    all_players = (
        [(p, team1_color) for p in team1_players] +
        [(p, team2_color) for p in team2_players]
    )
    all_players.sort(key=lambda x: x[0].get("score", 0), reverse=True)
    all_players = all_players[:MAX_ROWS]

    total_h = TOP_H + HDR_H + len(all_players) * ROW_H + PADDING

    max_kills  = max((p.get("kills", 0)           for p, _ in all_players), default=0)
    max_deaths = max((p.get("deaths", 0)          for p, _ in all_players), default=0)
    max_obj    = max((p.get("objective_score", 0) for p, _ in all_players), default=0)
    max_dmg    = max((p.get("damage_done", 0)     for p, _ in all_players), default=0)

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

    bbox_t1 = draw.textbbox((0, 0), f"{team1_name} ({team1_score})", font=f_teams)
    t1_y = y + (ROW1_H - (bbox_t1[3] - bbox_t1[1])) // 2
    draw.text((PADDING, t1_y), f"{team1_name} ({team1_score})", font=f_teams, fill=team1_color)

    map_text = map_name.upper()
    bbox_map = draw.textbbox((0, 0), map_text, font=f_map)
    draw.text((W - PADDING - (bbox_map[2] - bbox_map[0]), y), map_text, font=f_map, fill=WHITE)
    y += ROW1_H

    draw.text((PADDING, y), f"{team2_name} ({team2_score})", font=f_teams, fill=team2_color)

    winner = team1_name if team1_score > team2_score else team2_name
    series = f"{max(team1_score, team2_score)}-{min(team1_score, team2_score)}"
    after  = f" wins the series {series}!"
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
    draw.line([(PADDING, y + HDR_H - 1), (W - PADDING, y + HDR_H - 1)], fill=(120, 120, 150, 200), width=1)
    y += HDR_H

    for i, (p, color) in enumerate(all_players):
        text_y = y + (ROW_H - 22) // 2
        name   = p.get("name", "")
        kills  = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        obj    = p.get("objective_score", 0)
        dmg    = int(p.get("damage_done", 0))
        score  = p.get("score", 0)

        draw.text((C_NUM, text_y), f"{i+1}.", font=f_small, fill=GRAY)

        if name:
            draw.text((C_NAME,  text_y), name[:26], font=f_bold, fill=color)
            draw.text((C_SCORE, text_y), str(score),  font=f_reg, fill=WHITE)
            draw.text((C_KILLS, text_y), str(kills),  font=f_reg, fill=GREEN    if kills  == max_kills  and kills  > 0 else WHITE)
            draw.text((C_DEATH, text_y), str(deaths), font=f_reg, fill=STAT_RED if deaths == max_deaths and deaths > 0 else WHITE)
            draw.text((C_OBJ,   text_y), str(obj),    font=f_reg, fill=GREEN    if obj    == max_obj    and obj    > 0 else WHITE)
            draw.text((C_DMG,   text_y), str(dmg),    font=f_reg, fill=GREEN    if dmg    == int(max_dmg) and dmg  > 0 else WHITE)

        draw.line([(PADDING, y + ROW_H - 1), (W - PADDING, y + ROW_H - 1)], fill=(80, 80, 100, 100), width=1)
        y += ROW_H

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


if __name__ == "__main__":
    t1 = [
        {"name": "AraffyWappy",   "score": 3910, "kills": 29, "deaths": 21, "objective_score": 610, "damage_done": 3004},
        {"name": "LESHAWN",       "score": 3130, "kills": 23, "deaths": 23, "objective_score": 430, "damage_done": 2099},
        {"name": "TravisScottAl", "score": 2865, "kills": 20, "deaths": 21, "objective_score": 550, "damage_done": 1634},
        {"name": "mcyy",          "score": 2535, "kills": 23, "deaths": 31, "objective_score": 220, "damage_done": 1991},
    ]
    t2 = [
        {"name": "VollerPlays",   "score": 380,  "kills": 3,  "deaths": 1,  "objective_score": 30,  "damage_done": 214},
        {"name": "HypeZeus",      "score": 165,  "kills": 1,  "deaths": 2,  "objective_score": 90,  "damage_done": 95},
        {"name": "MemoMINI",      "score": 120,  "kills": 1,  "deaths": 3,  "objective_score": 40,  "damage_done": 80},
        {"name": "ECODOT",        "score": 90,   "kills": 0,  "deaths": 2,  "objective_score": 20,  "damage_done": 50},
    ]
    buf = draw_scoreboard(
        tournament_name="FRVR X NACK $700 4v4 Tournament",
        map_name="Sandstorm",
        team1_name="CEAF OWNERS",
        team1_score=2,
        team1_players=t1,
        team1_color=RED,
        team2_name="UFO",
        team2_score=0,
        team2_players=t2,
        team2_color=WHITE,
    )
    with open("/home/claude/test_scoreboard.png", "wb") as f:
        f.write(buf.read())
    print("Saved")