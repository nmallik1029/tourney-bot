from PIL import Image, ImageDraw, ImageFont
import io
import os

KRUNKER_FONT = os.path.join(os.path.dirname(__file__), "krunker_font.ttf")
FALLBACK_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def make_font(size):
    try:
        return ImageFont.truetype(KRUNKER_FONT, size)
    except Exception:
        try:
            return ImageFont.truetype(FALLBACK_BOLD, size)
        except Exception:
            return ImageFont.load_default()

# Colors
RED      = (220, 50, 50)
WHITE    = (255, 255, 255)
GOLD     = (241, 196, 15)
GRAY     = (160, 160, 175)
GREEN    = (80, 220, 80)
STAT_RED = (220, 60, 60)
DIVIDER  = (80, 80, 100, 120)  # RGBA semi-transparent

W       = 1100
PADDING = 32
ROW_H   = 48
HDR_H   = 36

C_NUM   = PADDING
C_NAME  = PADDING + 52
C_SCORE = 500
C_KILLS = 630
C_DEATH = 730
C_OBJ   = 830
C_DMG   = 950

BG_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "scoreboard_bg.png")


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

    # Find stat leaders
    max_kills  = max((p.get("kills", 0)            for p, _ in all_players), default=0)
    max_deaths = max((p.get("deaths", 0)           for p, _ in all_players), default=0)
    max_obj    = max((p.get("objective_score", 0)  for p, _ in all_players), default=0)
    max_dmg    = max((p.get("damage_done", 0)      for p, _ in all_players), default=0)

    n = len(all_players)
    top_h   = 155
    total_h = top_h + HDR_H + n * ROW_H + PADDING * 2

    # RGBA canvas — fully transparent background
    img = Image.new("RGBA", (W, total_h), (0, 0, 0, 0))

    # Optional background image
    if os.path.exists(BG_IMAGE_PATH):
        try:
            bg = Image.open(BG_IMAGE_PATH).convert("RGBA").resize((W, total_h))
            overlay = Image.new("RGBA", (W, total_h), (0, 0, 0, 120))
            bg = Image.alpha_composite(bg, overlay)
            img = Image.alpha_composite(img, bg)
        except Exception:
            pass

    draw = ImageDraw.Draw(img)

    f_tiny  = make_font(13)
    f_small = make_font(17)
    f_reg   = make_font(20)
    f_bold  = make_font(24)
    f_map   = make_font(54)
    f_teams = make_font(28)
    f_col   = make_font(17)

    y = PADDING

    # Tournament name top-left
    draw.text((PADDING, y), tournament_name, font=f_tiny, fill=GRAY)

    # Map name centered
    map_text = map_name.upper()
    bbox = draw.textbbox((0, 0), map_text, font=f_map)
    mx = (W - (bbox[2] - bbox[0])) // 2
    draw.text((mx, y - 8), map_text, font=f_map, fill=WHITE)
    y += 62

    # Team names + MATCH OVER
    t1_text = f"{team1_name} ({team1_score})"
    draw.text((PADDING, y), t1_text, font=f_teams, fill=team1_color)

    t2_text = f"({team2_score}) {team2_name}"
    bbox = draw.textbbox((0, 0), t2_text, font=f_teams)
    draw.text((W - PADDING - (bbox[2] - bbox[0]), y), t2_text, font=f_teams, fill=team2_color)
    y += 44

    # Winner line — team name in gold, rest in white
    winner = team1_name if team1_score > team2_score else team2_name
    series = f"{max(team1_score, team2_score)}-{min(team1_score, team2_score)}"

    before = f""
    mid = f"{winner}"
    after = f" wins the series {series}!"

    # Draw each part separately to color just the name
    x = PADDING
    bbox_b = draw.textbbox((0, 0), before, font=f_bold)
    bbox_m = draw.textbbox((0, 0), mid, font=f_bold)
    bbox_a = draw.textbbox((0, 0), after, font=f_bold)
    total_w = (bbox_b[2]-bbox_b[0]) + (bbox_m[2]-bbox_m[0]) + (bbox_a[2]-bbox_a[0])
    x = (W - total_w) // 2

    draw.text((x, y), before, font=f_bold, fill=WHITE)
    x += bbox_b[2] - bbox_b[0]
    draw.text((x, y), mid, font=f_bold, fill=GOLD)
    x += bbox_m[2] - bbox_m[0]
    draw.text((x, y), after, font=f_bold, fill=WHITE)
    y += 40

    # Column headers — no background, just text
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

    # Divider under headers
    draw.line([(PADDING, y + HDR_H - 1), (W - PADDING, y + HDR_H - 1)], fill=(120, 120, 150, 200), width=1)
    y += HDR_H

    # Player rows
    for i, (p, color) in enumerate(all_players):
        text_y = y + (ROW_H - 22) // 2

        kills  = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        obj    = p.get("objective_score", 0)
        dmg    = p.get("damage_done", 0)
        score  = p.get("score", 0)

        # Row number
        draw.text((C_NUM,   text_y), f"{i+1}.", font=f_small, fill=GRAY)

        # Player name in team color
        draw.text((C_NAME,  text_y), p.get("name", "")[:24], font=f_bold, fill=color)

        # Score always white
        draw.text((C_SCORE, text_y), str(score),  font=f_reg, fill=WHITE)

        # Kills — green if highest
        kills_color = GREEN if kills == max_kills and kills > 0 else WHITE
        draw.text((C_KILLS, text_y), str(kills),  font=f_reg, fill=kills_color)

        # Deaths — red if highest
        deaths_color = STAT_RED if deaths == max_deaths and deaths > 0 else WHITE
        draw.text((C_DEATH, text_y), str(deaths), font=f_reg, fill=deaths_color)

        # Obj — green if highest
        obj_color = GREEN if obj == max_obj and obj > 0 else WHITE
        draw.text((C_OBJ,   text_y), str(obj),    font=f_reg, fill=obj_color)

        # Dmg — green if highest
        dmg_color = GREEN if dmg == max_dmg and dmg > 0 else WHITE
        draw.text((C_DMG,   text_y), str(dmg),    font=f_reg, fill=dmg_color)

        # Divider line between rows
        draw.line(
            [(PADDING, y + ROW_H - 1), (W - PADDING, y + ROW_H - 1)],
            fill=(80, 80, 100, 100),
            width=1,
        )
        y += ROW_H

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


if __name__ == "__main__":
    t1 = [
        {"name": "TheseWalls", "score": 6655, "kills": 45, "deaths": 30, "objective_score": 1340, "damage_done": 4183},
        {"name": "PulseFN",    "score": 6085, "kills": 37, "deaths": 39, "objective_score": 1340, "damage_done": 4582},
        {"name": "Lvpez",      "score": 5920, "kills": 40, "deaths": 41, "objective_score": 1070, "damage_done": 3781},
        {"name": "rckyyyyyy",  "score": 5805, "kills": 37, "deaths": 30, "objective_score": 1540, "damage_done": 3659},
    ]
    t2 = [
        {"name": "VollerPlays", "score": 5950, "kills": 41, "deaths": 43, "objective_score": 1210, "damage_done": 4694},
        {"name": "HypeZeus",    "score": 5870, "kills": 41, "deaths": 50, "objective_score": 880,  "damage_done": 4926},
        {"name": "MemoMINI",    "score": 5745, "kills": 41, "deaths": 47, "objective_score": 910,  "damage_done": 4836},
        {"name": "ECODOT",      "score": 4890, "kills": 36, "deaths": 53, "objective_score": 920,  "damage_done": 4257},
    ]

    buf = draw_scoreboard(
        tournament_name="FRVR X NACK $700 4v4 Tournament",
        map_name="Bureau",
        team1_name="PPB",
        team1_score=2,
        team1_players=t1,
        team1_color=RED,
        team2_name="Kalashnikov",
        team2_score=1,
        team2_players=t2,
        team2_color=WHITE,
    )
    with open("/home/claude/test_scoreboard.png", "wb") as f:
        f.write(buf.read())
    print("Saved")
