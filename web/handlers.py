import io
import json
import os
import re
import urllib.parse
from pathlib import Path

import discord
from aiohttp import web

from core.bot_instance import bot
from core.config import SERVER_ID, ROLE_ID, RAILWAY_BASE
from core.storage import tournaments, active_matches, save_tournaments, _dashboard_tokens
from services.challonge import (
    challonge_get_matches,
    challonge_get_participants,
    challonge_create_tournament,
    challonge_add_participants,
    challonge_start,
    challonge_update_match,
)
from utils.helpers import get_categories
from views.pickban import PickBanView, create_match

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


# ── Krunker webhook ───────────────────────────────────────────────────────────
async def handle_krunker_webhook(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    raw_json = json.dumps(payload, indent=2)
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name="test-raw-data")
        if ch:
            for i in range(0, len(raw_json), 1990):
                await ch.send(f"```json\n{raw_json[i:i+1990]}\n```")

    event_type = payload.get("type")
    if event_type != "match_end":
        return web.Response(status=200, text="ok")

    teams = payload.get("teams", [])
    players = payload.get("players", [])
    winner_team_num = payload.get("winner")
    map_name = payload.get("map", "Unknown")

    if len(teams) < 2 or winner_team_num is None:
        return web.Response(status=200, text="ok")

    team1_name = teams[0]["name"]
    team2_name = teams[1]["name"]

    matched_tournament = None
    matched_team1 = None
    matched_team2 = None

    for t_id, t in tournaments.items():
        if t.get("open"):
            continue
        if not t.get("updates_channel_id"):
            continue

        registered_teams = t.get("teams", [])
        t1 = next((tm for tm in registered_teams if tm["team_name"].strip().lower() == team1_name.strip().lower()), None)
        t2 = next((tm for tm in registered_teams if tm["team_name"].strip().lower() == team2_name.strip().lower()), None)

        if t1 and t2:
            matched_tournament = t
            matched_team1 = t1
            matched_team2 = t2
            break

    if not matched_tournament:
        print(f"[Webhook] No tournament match found for teams: {team1_name} vs {team2_name}")
        return web.Response(status=200, text="ok")

    winner_krunker = next((tm for tm in teams if tm["team"] == winner_team_num), None)
    loser_krunker = next((tm for tm in teams if tm["team"] != winner_team_num), None)

    if not winner_krunker or not loser_krunker:
        return web.Response(status=200, text="ok")

    winner_discord = matched_team1 if matched_team1["team_name"].strip().lower() == winner_krunker["name"].strip().lower() else matched_team2
    loser_discord = matched_team1 if winner_discord == matched_team2 else matched_team2

    winner_score = winner_krunker["score"]
    loser_score = loser_krunker["score"]
    score_str = f"{winner_score}-{loser_score}"

    winner_players = sorted(
        [p for p in players if p["team"] == winner_team_num],
        key=lambda p: p.get("score", 0), reverse=True
    )
    loser_players = sorted(
        [p for p in players if p["team"] != winner_team_num],
        key=lambda p: p.get("score", 0), reverse=True
    )

    # Generate scoreboard image
    img_buf = None
    image_file = None
    try:
        from scoreboard import draw_scoreboard, RED, WHITE
        img_buf = draw_scoreboard(
            tournament_name=matched_tournament["name"],
            map_name=map_name,
            team1_name=winner_discord["team_name"],
            team1_score=winner_score,
            team1_players=winner_players,
            team1_color=RED,
            team2_name=loser_discord["team_name"],
            team2_score=loser_score,
            team2_players=loser_players,
            team2_color=WHITE,
        )
        image_file = discord.File(img_buf, filename="scoreboard.png")
        print(f"[Scoreboard] Image generated successfully")
    except Exception as e:
        print(f"[Scoreboard] Failed to generate image: {e}")
        import traceback
        traceback.print_exc()

    # Track series score and report to Challonge when series is decided
    active_match = next(
        (m for m in active_matches.values()
         if m.get("challonge_match_id")
         and {m["teams"][0]["name"].lower(), m["teams"][1]["name"].lower()}
         == {winner_discord["team_name"].lower(), loser_discord["team_name"].lower()}),
        None,
    )

    if active_match:
        if "scoreboard_images" not in active_match:
            active_match["scoreboard_images"] = []
        if img_buf:
            img_buf.seek(0)
            active_match["scoreboard_images"].append(img_buf.read())

        series = active_match.get("series_score", {})
        winner_key = winner_discord["team_name"].lower()
        loser_key = loser_discord["team_name"].lower()
        series[winner_key] = series.get(winner_key, 0) + 1
        series[loser_key] = series.get(loser_key, 0)
        active_match["series_score"] = series

        fmt = active_match.get("format", "bo1")
        wins_needed = {"bo1": 1, "bo3": 2, "bo5": 3}.get(fmt, 1)
        winner_series_wins = series[winner_key]
        loser_series_wins = series[loser_key]

        print(f"[Series] {winner_discord['team_name']} {winner_series_wins} - {loser_series_wins} {loser_discord['team_name']} ({fmt}, need {wins_needed})")

        match_ch = bot.get_channel(active_match.get("channel_id"))
        if match_ch:
            if winner_series_wins >= wins_needed:
                series_embed = discord.Embed(
                    title=f"{winner_discord['team_name']} wins!",
                    description=f"**Final Score: {winner_series_wins}-{loser_series_wins}**",
                    color=0xFFD700,
                )
                series_embed.set_footer(text="GGs! This channel will be cleaned up shortly.")
                await match_ch.send(embed=series_embed)
            else:
                map_num = winner_series_wins + loser_series_wins
                host_id = active_match.get("host_captain_id")

                view = PickBanView(match_id=active_match["match_id"])
                view.rebuild()

                map_embed = discord.Embed(
                    title=f"{winner_discord['team_name']} takes Map {map_num}!",
                    description=(
                        f"**Series: {winner_series_wins}-{loser_series_wins}**\n\n"
                        f"<@{host_id}>, host the next map using the button below."
                    ),
                    color=0x5865F2,
                )
                map_embed.set_footer(text="Everyone else: wait for the game link.")

                host_msg = await match_ch.send(
                    embed=map_embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                active_match["host_message_id"] = host_msg.id

        # When the series is decided, post all scoreboards together
        if winner_series_wins >= wins_needed:
            t1_name = active_match["teams"][0]["name"]
            t2_name = active_match["teams"][1]["name"]
            if t1_name.lower() == winner_key:
                series_winner = t1_name
                series_loser = t2_name
            else:
                series_winner = t2_name
                series_loser = t1_name

            updates_ch = bot.get_channel(active_match.get("updates_channel_id"))
            if updates_ch:
                stored_images = active_match.get("scoreboard_images", [])
                if stored_images:
                    files = []
                    for i, img_bytes in enumerate(stored_images):
                        buf = io.BytesIO(img_bytes)
                        buf.seek(0)
                        files.append(discord.File(buf, filename=f"map{i+1}.png"))
                    await updates_ch.send(
                        f"**{series_winner}** won **{winner_series_wins}-{loser_series_wins}** against **{series_loser}**",
                        files=files,
                    )
                else:
                    await updates_ch.send(
                        f"**{series_winner}** won **{winner_series_wins}-{loser_series_wins}** against **{series_loser}**"
                    )
                print(f"[Webhook] Posted series result: {series_winner} {winner_series_wins}-{loser_series_wins} {series_loser}")

            # Report to Challonge
            challonge_id = active_match.get("challonge_id")
            ch_match_id = active_match.get("challonge_match_id")
            participant_map = active_match.get("challonge_participant_map", {})
            winner_ch_pid = participant_map.get(winner_discord.get("team_id"))

            if challonge_id and ch_match_id and winner_ch_pid:
                try:
                    open_matches = await challonge_get_matches(challonge_id, state="all")
                    ch_match = next((m for m in open_matches if m["id"] == ch_match_id), None)
                    if ch_match:
                        if ch_match["player1_id"] == winner_ch_pid:
                            csv = f"{winner_series_wins}-{loser_series_wins}"
                        else:
                            csv = f"{loser_series_wins}-{winner_series_wins}"
                        await challonge_update_match(challonge_id, ch_match_id, csv, winner_ch_pid)
                        print(f"[Challonge] Series decided -- reported {csv} for match {ch_match_id}")
                except Exception as e:
                    print(f"[Challonge] Failed to report result: {e}")

            # Clean up the active match
            match_id = active_match.get("match_id")
            if match_id and match_id in active_matches:
                del active_matches[match_id]
    else:
        # No active match found (standalone game) -- post immediately
        updates_ch_id = matched_tournament.get("updates_channel_id")
        if updates_ch_id:
            updates_ch = bot.get_channel(updates_ch_id)
            if updates_ch:
                if image_file:
                    await updates_ch.send(
                        f"**{winner_discord['team_name']}** won **{score_str}** against **{loser_discord['team_name']}**",
                        file=image_file,
                    )
                else:
                    await updates_ch.send(
                        f"**{winner_discord['team_name']}** won **{score_str}** against **{loser_discord['team_name']}**"
                    )

    return web.Response(status=200, text="ok")


# ── Launch redirect ───────────────────────────────────────────────────────────
async def handle_launch(request: web.Request) -> web.Response:
    client = request.query.get("client", "glorp")
    params = {k: v for k, v in request.query.items() if k != "client"}
    query = urllib.parse.urlencode(params)
    if client == "crankshaft":
        target_url = f"crankshaft://game?{query}"
    else:
        target_url = f"glorp://game?{query}"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>Launching {client.title()}...</title>
  <style>
    body {{ font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #1e1f22; color: #fff; }}
    p {{ font-size: 1.2rem; }}
  </style>
</head>
<body>
  <p>Launching {client.title()}... You can close this tab.</p>
  <script>window.location = "{target_url}";</script>
</body>
</html>"""
    return web.Response(content_type="text/html", text=html)


# ── Admin bracket page ────────────────────────────────────────────────────────
async def handle_admin_bracket(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.Response(status=403, text="Invalid tournament or token.")

    html = _load_template("bracket.html").replace("{{TOURNAMENT_ID}}", t_id).replace("{{TOKEN}}", token)
    return web.Response(content_type="text/html", text=html)


async def handle_api_tournament(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    challonge_id = t.get("challonge_id")
    if not challonge_id:
        return web.json_response({"error": "no bracket"}, status=404)

    try:
        matches = await challonge_get_matches(challonge_id, state="all")
        participants = await challonge_get_participants(challonge_id)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    p_map = {p["id"]: p["name"] for p in participants}

    started_ch_ids = {m.get("challonge_match_id") for m in active_matches.values() if m.get("challonge_match_id")}

    match_list = []
    for m in matches:
        match_list.append({
            "id": m["id"],
            "round": m.get("round", 0),
            "identifier": m.get("identifier", ""),
            "state": m["state"],
            "player1": p_map.get(m.get("player1_id"), None),
            "player2": p_map.get(m.get("player2_id"), None),
            "player1_id": m.get("player1_id"),
            "player2_id": m.get("player2_id"),
            "scores_csv": m.get("scores_csv", ""),
            "winner_id": m.get("winner_id"),
            "started_in_discord": m["id"] in started_ch_ids,
        })

    return web.json_response({
        "name": t["name"],
        "challonge_url": t.get("challonge_url", ""),
        "matches": match_list,
    })


async def handle_api_start_match(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    t_id = body.get("tournament_id", "").upper()
    token = body.get("token", "")
    ch_match_id = body.get("challonge_match_id")
    fmt = body.get("format", "bo1")
    casted = body.get("casted", False)

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    if fmt not in ("bo1", "bo3", "bo5"):
        return web.json_response({"error": "invalid format"}, status=400)

    challonge_id = t.get("challonge_id")
    if not challonge_id:
        return web.json_response({"error": "no bracket"}, status=404)

    try:
        matches = await challonge_get_matches(challonge_id, state="open")
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    ch_match = next((m for m in matches if m["id"] == ch_match_id), None)
    if not ch_match:
        return web.json_response({"error": "match not found or not open"}, status=404)

    participant_map = t.get("challonge_participant_map", {})
    ch_to_local = {}
    for team_id, ch_pid in participant_map.items():
        team_dict = next((tm for tm in t["teams"] if tm["team_id"] == team_id), None)
        if team_dict:
            ch_to_local[ch_pid] = team_dict

    t1_dict = ch_to_local.get(ch_match.get("player1_id"))
    t2_dict = ch_to_local.get(ch_match.get("player2_id"))
    if not t1_dict or not t2_dict:
        return web.json_response({"error": "could not resolve teams"}, status=404)

    guild = bot.get_guild(SERVER_ID)
    if not guild:
        return web.json_response({"error": "bot not in target guild"}, status=500)

    try:
        ch = await create_match(guild, t1_dict, t2_dict, t, fmt, challonge_match_id=ch_match_id, casted=casted)
        return web.json_response({"ok": True, "channel": ch.name})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── Seeding pages ──────────────────────────────────────────────────────────────
async def handle_admin_seeding(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")
    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.Response(status=403, text="Invalid tournament or token.")
    if t.get("challonge_id"):
        raise web.HTTPFound(f"/admin/{t_id}?token={token}")
    html = _load_template("seeding.html").replace("{{TOURNAMENT_ID}}", t_id).replace("{{TOKEN}}", token)
    return web.Response(content_type="text/html", text=html)


async def handle_api_seeding(request: web.Request) -> web.Response:
    t_id = request.match_info["tournament_id"].upper()
    token = request.query.get("token", "")
    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    team_list = []
    for tm in t.get("teams", []):
        igns = [p.get("ign", "Unknown") for p in tm.get("players", [])]
        team_list.append({
            "team_id": tm["team_id"],
            "team_name": tm["team_name"],
            "players": igns,
        })
    return web.json_response({"name": t["name"], "teams": team_list})


async def handle_api_confirm_seeding(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    t_id = body.get("tournament_id", "").upper()
    token = body.get("token", "")
    team_order = body.get("team_order", [])

    t = tournaments.get(t_id)
    if not t or t.get("admin_token") != token:
        return web.json_response({"error": "forbidden"}, status=403)

    if t.get("challonge_id"):
        return web.json_response({"error": "bracket already created"}, status=400)

    # Reorder teams
    team_map = {tm["team_id"]: tm for tm in t.get("teams", [])}
    seeded_teams = []
    for tid in team_order:
        if tid in team_map:
            seeded_teams.append(team_map[tid])
    for tm in t.get("teams", []):
        if tm["team_id"] not in [st["team_id"] for st in seeded_teams]:
            seeded_teams.append(tm)

    t["open"] = False
    t["teams"] = seeded_teams
    save_tournaments()

    guild = bot.get_guild(SERVER_ID)
    if not guild:
        return web.json_response({"error": "bot not in guild"}, status=500)

    staff_role = guild.get_role(ROLE_ID)
    t_cat, a_cat = get_categories(guild)

    read_only = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
    }
    admin_perms = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff_role:
        admin_perms[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    try:
        updates_channel = await guild.create_text_channel(name="tournament-updates", overwrites=read_only, category=t_cat)
        admin_channel = await guild.create_text_channel(name="tournament-admin", overwrites=admin_perms, category=a_cat)
        caster_channel = await guild.create_text_channel(name="caster-links", overwrites=admin_perms, category=a_cat)
        t["updates_channel_id"] = updates_channel.id
        t["admin_channel_id"] = admin_channel.id
        t["caster_channel_id"] = caster_channel.id
        save_tournaments()
    except Exception as e:
        return web.json_response({"error": f"channel creation failed: {e}"}, status=500)

    # Create per-team roles, categories, and channels
    t["team_roles"] = {}
    t["team_channels"] = {}
    t["team_categories"] = {}
    for tm in seeded_teams:
        team_name = tm["team_name"]
        try:
            role = await guild.create_role(name=team_name, mentionable=True)
            tm["role_id"] = role.id
            t["team_roles"][tm["team_id"]] = role.id

            for player in tm.get("players", []):
                pid = player.get("discord_id")
                if pid:
                    member = guild.get_member(pid)
                    if member:
                        try:
                            await member.add_roles(role)
                            print(f"[Teams] Assigned role '{team_name}' to {member.display_name}")
                        except Exception as e:
                            print(f"[Teams] Failed to assign role to {pid}: {e}")

            team_perms = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True),
            }
            if staff_role:
                team_perms[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True)

            team_category = await guild.create_category(name=team_name, overwrites=team_perms)
            t["team_categories"][tm["team_id"]] = team_category.id

            text_ch = await guild.create_text_channel(name=team_name, category=team_category)
            voice_ch = await guild.create_voice_channel(name=team_name, category=team_category)
            t["team_channels"][tm["team_id"]] = {"text": text_ch.id, "voice": voice_ch.id}
            print(f"[Teams] Created role + category + channels for {team_name}")
        except Exception as e:
            print(f"[Teams] Error creating role/channels for {team_name}: {e}")
    save_tournaments()

    # Create Challonge bracket
    team_count = len(seeded_teams)
    ch_name = f"krunkerbiweekly-{t['name']}"
    slug = re.sub(r"[^a-z0-9]", "_", ch_name.lower()).strip("_")[:60]
    try:
        print(f"[Challonge] Creating tournament: {ch_name} ({slug})")
        ch_tourney = await challonge_create_tournament(ch_name, slug)
        challonge_id = ch_tourney["id"]
        t["challonge_id"] = challonge_id
        t["challonge_url"] = ch_tourney.get("full_challonge_url", f"https://krunker-biweeklies.challonge.com/{slug}")
        print(f"[Challonge] Tournament created: {challonge_id}")

        participants = [
            {"name": tm["team_name"], "seed": i + 1, "misc": tm["team_id"]}
            for i, tm in enumerate(seeded_teams)
        ]
        print(f"[Challonge] Adding {len(participants)} participants...")
        added = await challonge_add_participants(challonge_id, participants)
        print(f"[Challonge] Participants added: {len(added)}")

        participant_map = {}
        if isinstance(added, list):
            for entry in added:
                p = entry.get("participant", entry)
                if p.get("misc"):
                    participant_map[p["misc"]] = p["id"]
        t["challonge_participant_map"] = participant_map

        await challonge_start(challonge_id)
        save_tournaments()
        print(f"[Challonge] Tournament started successfully")

        bracket_msg = f"**{t['name']}** has started with {team_count} teams!\n**Bracket:** {t['challonge_url']}"
    except Exception as e:
        print(f"[Challonge] Error: {e}")
        import traceback
        traceback.print_exc()
        bracket_msg = f"**{t['name']}** has started with {team_count} teams.\n\u26a0\ufe0f Challonge bracket creation failed: {e}"

    signups_ch = guild.get_channel(t.get("signups_channel_id"))
    if signups_ch:
        await signups_ch.send(f"Sign-ups closed -- {team_count} team{'s' if team_count != 1 else ''} registered.")

    updates_ch = guild.get_channel(t.get("updates_channel_id"))
    if updates_ch:
        await updates_ch.send(bracket_msg)

    return web.json_response({"ok": True})


# ── Dashboard ──────────────────────────────────────────────────────────────────
def _valid_dashboard_token(token: str) -> bool:
    if not token:
        return False
    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if dashboard_token and token == dashboard_token:
        return True
    if token in _dashboard_tokens:
        return True
    return any(t.get("admin_token") == token for t in tournaments.values())


async def handle_dashboard(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not _valid_dashboard_token(token):
        return web.Response(status=403, text="Invalid token.")
    html = _load_template("dashboard.html").replace("{{TOKEN}}", token)
    return web.Response(content_type="text/html", text=html)


async def handle_api_dashboard(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not _valid_dashboard_token(token):
        return web.json_response({"error": "forbidden"}, status=403)

    result = []
    for t_id, t in tournaments.items():
        teams_data = []
        for tm in t.get("teams", []):
            igns = [p.get("ign", "Unknown") for p in tm.get("players", [])]
            teams_data.append({"team_name": tm["team_name"], "team_id": tm["team_id"], "players": igns})

        match_data = []
        for mid, m in active_matches.items():
            if m.get("tournament_id") == t_id:
                t1 = m["teams"][0]["name"] if m.get("teams") else "?"
                t2 = m["teams"][1]["name"] if m.get("teams") and len(m["teams"]) > 1 else "?"
                series = m.get("series_score", {})
                fmt = m.get("format", "bo1")
                if series:
                    s1 = series.get(t1.lower(), 0)
                    s2 = series.get(t2.lower(), 0)
                    score_str = f"{s1}-{s2}"
                else:
                    score_str = None
                match_data.append({"team1": t1, "team2": t2, "format": fmt, "series_score": score_str})

        admin_panel_url = None
        if t.get("admin_token") and t.get("challonge_id"):
            admin_panel_url = f"{RAILWAY_BASE}/admin/{t_id}?token={t['admin_token']}"

        result.append({
            "id": t_id,
            "name": t.get("name", t_id),
            "open": t.get("open", False),
            "ended": t.get("ended", False),
            "team_size": t.get("team_size", "?"),
            "organizer_name": t.get("organizer_name", "Unknown"),
            "challonge_id": t.get("challonge_id"),
            "challonge_url": t.get("challonge_url"),
            "admin_panel_url": admin_panel_url,
            "teams": teams_data,
            "matches": match_data,
        })

    return web.json_response({"tournaments": result})
