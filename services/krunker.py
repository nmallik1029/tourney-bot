"""Fetch a Krunker player's clan tag + verified status for the result scoreboard.

Krunker only exposes profile data over an anti-bot WebSocket (msgpack) that rejects plain
server connections, so we drive a real headless Chromium (Playwright): it loads the
player's social profile and we sniff the page's own WebSocket frames, reading:

  - `player_clan`     -> the clan tag
  - `player_featured` -> verified (the blue checkmark; works even for clanless players)

One profile fetch per player gives both. Everything is BEST-EFFORT: any failure
(Playwright missing, captcha, timeout) just yields no clan / not verified -- it must never
break the match. Results are cached for hours and the browser is launched on demand and
closed right after, so it isn't burning RAM 24/7 (matters on Railway's usage billing).
"""

import asyncio
import time

try:
    import msgpack
except Exception:  # pragma: no cover
    msgpack = None

# How long a looked-up clan/verified result is reused before we fetch it again. Clans
# change rarely, so a short window keeps things fresh without re-loading the same profiles
# every game (which, from a datacenter IP, raises the risk Krunker captcha-blocks us).
#   3600  = re-check at most once an hour per player (recommended)
#   0     = look up every player every game (freshest, but most requests / block risk)
CACHE_TTL = 3600
_LOOKUP_TIMEOUT = 22
_CONCURRENCY = 3
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# username_lower -> ({"clan": str, "verified": bool}, fetched_at)
_cache: dict[str, tuple[dict, float]] = {}
_lock = asyncio.Lock()


def _key(s: str) -> str:
    return (s or "").strip().lower()


def cached_meta(username: str) -> dict | None:
    e = _cache.get(_key(username))
    if e and (time.time() - e[1]) < CACHE_TTL:
        return e[0]
    return None


def _decode(payload):
    if msgpack is None:
        return None
    if isinstance(payload, str):
        payload = payload.encode("latin1", "ignore")
    try:
        up = msgpack.Unpacker(raw=False)
        up.feed(payload)
        return next(iter(up))
    except Exception:
        return None


async def _fetch_profile(ctx, username: str) -> dict:
    """Load one profile; return {"clan": str, "verified": bool, "_ok": bool}."""
    holder = {"obj": None}
    page = await ctx.new_page()

    def on_ws(ws):
        def on_frame(payload):
            data = _decode(payload)
            if isinstance(data, list) and len(data) >= 4 and data[1] == "profile" \
                    and isinstance(data[3], dict):
                holder["obj"] = data[3]
        ws.on("framereceived", on_frame)

    page.on("websocket", on_ws)
    try:
        await page.goto(
            f"https://krunker.io/social.html?p=profile&q={username}",
            wait_until="domcontentloaded", timeout=25000,
        )
        deadline = time.time() + _LOOKUP_TIMEOUT
        while holder["obj"] is None and time.time() < deadline:
            await asyncio.sleep(0.4)
    except Exception as e:
        print(f"[Krunker] profile fetch failed for {username}: {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if holder["obj"] is None:
        return {"clan": "", "verified": False, "_ok": False}

    prof = holder["obj"]
    try:
        featured = int(prof.get("player_featured") or 0)
    except (TypeError, ValueError):
        featured = 0
    return {"clan": (prof.get("player_clan") or "").strip(), "verified": featured > 0, "_ok": True}


async def prefetch_players(usernames: list[str]) -> dict[str, dict]:
    """Fetch + cache {clan, verified} for these Krunker usernames (skips fresh entries).
    Returns {username_lower: {"clan": str, "verified": bool}}. Best-effort."""
    need = [u for u in {u.strip() for u in usernames if u and u.strip()}
            if cached_meta(u) is None]

    if need:
        async with _lock:
            need = [u for u in need if cached_meta(u) is None]
            if need:
                try:
                    from playwright.async_api import async_playwright
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(
                            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                        try:
                            ctx = await browser.new_context(user_agent=_UA)
                            sem = asyncio.Semaphore(_CONCURRENCY)

                            async def go(u):
                                async with sem:
                                    meta = await _fetch_profile(ctx, u)
                                    ok = meta.pop("_ok", False)
                                    if ok:
                                        _cache[_key(u)] = (meta, time.time())

                            await asyncio.gather(*(go(u) for u in need))
                        finally:
                            await browser.close()
                except Exception as e:
                    print(f"[Krunker] prefetch error (clans skipped): {e}")

                fetched = {u: cached_meta(u) for u in need}
                ok_count = sum(1 for m in fetched.values() if m is not None)
                tagged = sum(1 for m in fetched.values() if m and m.get("clan"))
                checked = sum(1 for m in fetched.values() if m and m.get("verified"))
                print(
                    f"[Krunker] prefetch complete: profiles={ok_count}/{len(need)} "
                    f"clans={tagged} verified={checked}"
                )

    out = {}
    for u in usernames:
        if not u or not u.strip():
            continue
        meta = cached_meta(u)
        if meta is not None:
            out[_key(u)] = meta
    return out


async def fetch_player(username: str) -> dict:
    """Single-player lookup (used when a sub is brought in)."""
    res = await prefetch_players([username])
    return res.get(_key(username), {"clan": "", "verified": False})
