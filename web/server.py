from aiohttp import web
from core.config import WEBHOOK_PORT
from web.handlers import (
    handle_krunker_webhook,
    handle_launch,
    handle_admin_bracket,
    handle_admin_seeding,
    handle_api_tournament,
    handle_api_start_match,
    handle_api_seeding,
    handle_api_confirm_seeding,
    handle_dashboard,
    handle_api_dashboard,
    handle_match_history,
)


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/krunker", handle_krunker_webhook)
    app.router.add_get("/launch", handle_launch)
    app.router.add_get("/admin/{tournament_id}/seeding", handle_admin_seeding)
    app.router.add_get("/admin/{tournament_id}", handle_admin_bracket)
    app.router.add_get("/api/seeding/{tournament_id}", handle_api_seeding)
    app.router.add_post("/api/confirm-seeding", handle_api_confirm_seeding)
    app.router.add_get("/api/tournament/{tournament_id}", handle_api_tournament)
    app.router.add_post("/api/start-match", handle_api_start_match)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/api/dashboard", handle_api_dashboard)
    app.router.add_get("/api/match-history/{tournament_id}", handle_match_history)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"Webhook server running on port {WEBHOOK_PORT}")
