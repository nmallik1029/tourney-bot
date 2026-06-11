import time
from pathlib import Path

from aiohttp import web

from core.bot_instance import bot
from core.config import SERVER_ID
from pug.storage import (
    pug_data,
    pug_queue,
    pug_matches,
    save_pug_data,
    set_noadd,
    is_noadded,
    get_noadd_info,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _valid_token(token: str) -> bool:
    return bool(token) and token == pug_data.get("dashboard_token")


def _display_name(discord_id) -> str:
    guild = bot.get_guild(SERVER_ID)
    if guild:
        member = guild.get_member(int(discord_id))
        if member:
            return member.display_name
    return str(discord_id)


# Pages
async def handle_pug_dashboard(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not _valid_token(token):
        return web.Response(status=403, text="Invalid token.")
    html = _load_template("pug_dashboard.html").replace("{{TOKEN}}", token)
    return web.Response(content_type="text/html", text=html)


async def handle_api_pug_dashboard(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    if not _valid_token(token):
        return web.json_response({"error": "forbidden"}, status=403)

    players = []
    for did, p in sorted(pug_data["players"].items(), key=lambda kv: kv[1].get("elo", 0), reverse=True):
        noadded = is_noadded(did)
        ninfo = get_noadd_info(did) if noadded else None
        players.append({
            "discord_id": did,
            "name": _display_name(did),
            "elo": p.get("elo", 0),
            "wins": p.get("wins", 0),
            "losses": p.get("losses", 0),
            "usernames": p.get("usernames", []),
            "noadded": noadded,
            "noadd_reason": (ninfo or {}).get("reason", ""),
            "noadd_until": (ninfo or {}).get("until"),
        })

    queue = [{"discord_id": str(pid), "name": _display_name(pid)} for pid in pug_queue]

    matches = []
    for m in pug_matches.values():
        matches.append({
            "name": m.get("name"),
            "phase": m.get("phase"),
            "players": [_display_name(p) for p in m.get("players", [])],
            "checked_in": len(m.get("checked_in", set())),
            "size": len(m.get("players", [])),
        })

    return web.json_response({
        "players": players,
        "queue": queue,
        "matches": matches,
        "noadds": [
            {
                "discord_id": n,
                "name": _display_name(n),
                "reason": info.get("reason", ""),
                "until": info.get("until"),
            }
            for n, info in pug_data["noadds"].items()
        ],
    })


async def handle_api_pug_action(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = body.get("token", "")
    if not _valid_token(token):
        return web.json_response({"error": "forbidden"}, status=403)

    action = body.get("action", "")
    target = body.get("discord_id", "").strip()
    admin = (body.get("admin") or "Dashboard").strip() or "Dashboard"
    reason = body.get("reason", "")

    def _until():
        dur = body.get("duration_hours")
        if dur not in (None, "", 0, "0"):
            try:
                return time.time() + float(dur) * 3600
            except (TypeError, ValueError):
                return None
        return None

    if action == "create_queue":
        guild = bot.get_guild(SERVER_ID)
        if not guild:
            return web.json_response({"error": "bot not in guild"}, status=500)
        from views.pug_queue import refresh_queue_embed
        from pug.match import pop_queue
        # Force a pop with whoever is queued (min 2, for testing); else just refresh.
        if len(pug_queue) >= 2:
            await pop_queue(guild, bot, force=True)
        await refresh_queue_embed(bot)
        return web.json_response({"ok": True})

    # Moderation actions need a target Discord ID
    if not target.isdigit():
        return web.json_response({"error": "valid discord_id required"}, status=400)
    tid = int(target)

    if action == "noadd":
        set_noadd(tid, True, reason=reason, until=_until(), admin=admin)
        if tid in pug_queue:
            pug_queue.remove(tid)
    elif action == "unnoadd":
        set_noadd(tid, False, admin=admin)
    else:
        return web.json_response({"error": "unknown action"}, status=400)

    from views.pug_queue import refresh_queue_embed
    await refresh_queue_embed(bot)
    return web.json_response({"ok": True})
