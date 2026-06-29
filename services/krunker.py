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
import os
import time
from urllib.parse import urlparse

try:
    import msgpack
except Exception:  # pragma: no cover
    msgpack = None

# How long a looked-up clan/verified result is reused before we fetch it again. Clans
# change rarely, and every fetch goes through a metered residential proxy, so we keep this
# long -- combined with the persisted cache it means each player costs ~one lookup per day.
#   86400 = re-check at most once a day per player (recommended)
#   3600  = hourly (fresher, ~24x the proxy bandwidth)
#   0     = look up every player every game (freshest, most bandwidth)
CACHE_TTL = 86400
_LOOKUP_TIMEOUT = 22
_CONCURRENCY = 3

# Resource types the profile lookup doesn't need: the clan/verified data arrives over a
# WebSocket once the page's JS runs, so we abort images/fonts/CSS/media before they download.
# That cuts each lookup from a few MB to a few hundred KB of (metered) proxy traffic.
_BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# username_lower -> ({"clan": str, "verified": bool}, fetched_at)
_cache: dict[str, tuple[dict, float]] = {}
_lock = asyncio.Lock()
_hydrated = False  # whether we've loaded the persisted cache from storage yet


async def _route_block(route):
    """Abort heavy resource requests; let everything else (document/script/xhr/ws) through."""
    try:
        if route.request.resource_type in _BLOCK_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


def hydrate_cache(persisted: dict) -> None:
    """Seed the in-memory cache from a persisted snapshot ({key: {clan, verified, ts}})."""
    if not isinstance(persisted, dict):
        return
    for k, v in persisted.items():
        if isinstance(v, dict):
            try:
                ts = float(v.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            _cache[_key(k)] = ({"clan": v.get("clan", ""), "verified": bool(v.get("verified"))}, ts)


def export_cache() -> dict:
    """A JSON-serializable snapshot of the cache for persistence."""
    return {k: {"clan": meta.get("clan", ""), "verified": bool(meta.get("verified")), "ts": ts}
            for k, (meta, ts) in _cache.items()}


def _ensure_hydrated() -> None:
    """Load the persisted clan cache once, so a fresh process (e.g. after a Railway redeploy)
    reuses prior lookups instead of re-fetching everyone through the metered proxy."""
    global _hydrated
    if _hydrated:
        return
    _hydrated = True
    try:
        from pug.storage import load_clan_cache
        hydrate_cache(load_clan_cache())
    except Exception as e:
        print(f"[Krunker] clan cache hydrate failed: {e}")


# Krunker's social page serves profile data over an anti-bot WebSocket that silently blocks
# datacenter IPs (so on Railway every lookup comes back empty -- the page loads but no profile
# frame ever arrives). Routing Chromium through a RESIDENTIAL proxy makes the requests look
# like a normal home connection and gets the data through. Set KRUNKER_PROXY to a proxy URL,
# e.g.  http://user:pass@gate.smartproxy.com:7000  (left unset -> no proxy, tags just omitted).
KRUNKER_PROXY = os.environ.get("KRUNKER_PROXY", "").strip()


def _proxy_settings() -> dict | None:
    """Parse KRUNKER_PROXY into Playwright's proxy dict, or None if unset/unparseable."""
    if not KRUNKER_PROXY:
        return None
    try:
        u = urlparse(KRUNKER_PROXY)
        if not u.hostname:
            return None
        scheme = u.scheme or "http"
        server = f"{scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
        proxy = {"server": server}
        if u.username:
            proxy["username"] = u.username
        if u.password:
            proxy["password"] = u.password
        return proxy
    except Exception:
        return None


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
    _ensure_hydrated()
    need = [u for u in {u.strip() for u in usernames if u and u.strip()}
            if cached_meta(u) is None]

    if need:
        async with _lock:
            need = [u for u in need if cached_meta(u) is None]
            if need:
                try:
                    from playwright.async_api import async_playwright
                    proxy = _proxy_settings()
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(
                            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                        try:
                            ctx = await browser.new_context(
                                user_agent=_UA, **({"proxy": proxy} if proxy else {}))
                            await ctx.route("**/*", _route_block)  # skip images/fonts/CSS/media
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
                    f"clans={tagged} verified={checked} "
                    f"proxy={'on' if _proxy_settings() else 'off'}"
                )

                # Persist only when we actually learned something, so a fully-blocked sweep
                # doesn't spend a Gist write (and risk a 409) saving an unchanged cache.
                if ok_count:
                    try:
                        from pug.storage import save_clan_cache
                        save_clan_cache(export_cache())
                    except Exception as e:
                        print(f"[Krunker] clan cache persist failed: {e}")

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
