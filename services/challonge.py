"""Challonge Connect (API v2.1) client.

Challonge retired the old v1 API-key auth for new accounts, so this talks to the v2.1
"Connect" API: OAuth2 (client-credentials grant) for a bearer token, JSON:API request
and response bodies, and a v2.1 base URL.

Setup (env vars), from the Connect app at https://connect.challonge.com:
  CHALLONGE_CLIENT_ID
  CHALLONGE_CLIENT_SECRET

Alternative:
  CHALLONGE_API_KEY

The public functions keep the same names/return shapes the rest of the bot expects:
each resource is flattened from JSON:API ({id, type, attributes}) back into a flat dict
(attributes + a string `id`), so callers reading `m["player1_id"]`, `p["misc"]`,
`t["full_challonge_url"]`, etc. keep working. All ids are coerced to strings so id
comparisons across the bot stay consistent (v2 returns ids as strings).
"""

import asyncio
import json
import os
import time

import aiohttp

CLIENT_ID = os.environ.get("CHALLONGE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CHALLONGE_CLIENT_SECRET", "")
API_KEY = os.environ.get("CHALLONGE_API_KEY", "")

OAUTH_TOKEN_URL = "https://api.challonge.com/oauth/token"
API_BASE = "https://api.challonge.com/v2.1"
SCOPES = ("me communities:manage tournaments:read tournaments:write participants:read "
          "participants:write matches:read matches:write")

# Cached bearer token (tokens last ~1 week; we refresh a minute early).
_token = {"value": "", "expires_at": 0.0}
_token_lock = asyncio.Lock()


async def _get_token() -> str:
    now = time.time()
    if _token["value"] and now < _token["expires_at"]:
        return _token["value"]
    async with _token_lock:
        # Re-check inside the lock in case another caller just refreshed.
        if _token["value"] and time.time() < _token["expires_at"]:
            return _token["value"]
        if not CLIENT_ID or not CLIENT_SECRET:
            raise Exception(
                "Challonge not configured: set CHALLONGE_CLIENT_ID and "
                "CHALLONGE_CLIENT_SECRET (Connect OAuth app)."
            )
        form = {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": SCOPES,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(OAUTH_TOKEN_URL, data=form) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    print(f"[Challonge] OAuth token error {resp.status}: {text[:400]}")
                    raise Exception(f"OAuth token error {resp.status}")
                data = json.loads(text)
        _token["value"] = data["access_token"]
        _token["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
        return _token["value"]


def _headers(token: str) -> dict:
    auth = API_KEY or f"Bearer {token}"
    return {
        "Authorization": auth,
        "Authorization-Type": "v2",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/json",
    }


async def _request(method: str, path: str, *, body: dict | None = None, params: dict | None = None):
    token = "" if API_KEY else await _get_token()
    url = f"{API_BASE}{path}"
    payload = json.dumps(body) if body is not None else None
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, params=params or {}, data=payload, headers=_headers(token)
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                print(f"[Challonge] {method} {path} -> {resp.status}: {text[:500]}")
                raise Exception(f"{resp.status}, {text[:200]}")
            if not text:
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                raise Exception(f"Non-JSON response: {text[:200]}")


def _flatten(item: dict) -> dict:
    """JSON:API resource ({id, type, attributes}) -> flat dict (attributes + string id),
    matching the old v1 shape. Id-bearing fields are stringified for consistent compares."""
    attrs = dict(item.get("attributes", {}))
    out = {**attrs, "id": str(item.get("id"))}
    for k in ("player1_id", "player2_id", "winner_id", "loser_id"):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


async def challonge_create_tournament(name: str, url_slug: str, subdomain: str = "") -> dict:
    """Create a tournament. `subdomain` (a Community subdomain/permalink) files it under
    that Community via the community_id query param; empty -> the account's main page."""
    body = {"data": {"type": "tournament", "attributes": {
        "name": name,
        "url": url_slug,
        "tournament_type": "double elimination",
        "game_name": "Krunker",
        "private": False,
    }}}
    params = {"community_id": subdomain} if subdomain else None
    data = await _request("POST", "/tournaments.json", body=body, params=params)
    return _flatten(data["data"])


async def challonge_add_participants(challonge_id, teams_with_seeds: list[dict]) -> list:
    """teams_with_seeds: [{"name": "...", "seed": 1, "misc": "team_id"}, ...]. Added one at
    a time (v2 single-create) so the team_id<->participant_id mapping stays exact."""
    added = []
    for t in teams_with_seeds:
        body = {"data": {"type": "participant", "attributes": {
            "name": t["name"],
            "seed": int(t["seed"]),
            "misc": t.get("misc", ""),
        }}}
        data = await _request("POST", f"/tournaments/{challonge_id}/participants.json", body=body)
        added.append(_flatten(data["data"]))
    return added


async def challonge_start(challonge_id) -> dict:
    body = {"data": {"type": "TournamentState", "attributes": {"state": "start"}}}
    data = await _request("PUT", f"/tournaments/{challonge_id}/change_state.json", body=body)
    return data.get("data", data)


async def challonge_get_matches(challonge_id, state="open") -> list:
    # v1 used state="all" to mean everything; v2 returns all when the param is omitted.
    params = None if state in (None, "all") else {"state": state}
    data = await _request("GET", f"/tournaments/{challonge_id}/matches.json", params=params)
    items = data.get("data", [])
    return [_flatten(m) for m in items] if isinstance(items, list) else []


async def challonge_get_participants(challonge_id) -> list:
    data = await _request("GET", f"/tournaments/{challonge_id}/participants.json")
    items = data.get("data", [])
    return [_flatten(p) for p in items] if isinstance(items, list) else []


async def challonge_update_match(challonge_id, match_id, player1_id, player2_id,
                                 scores_csv: str, winner_id) -> dict:
    """Report a single-set result. `scores_csv` is "p1score-p2score" (already oriented to
    player1 then player2); `winner_id` is the advancing participant."""
    parts = str(scores_csv).split("-")
    s1 = parts[0].strip() if len(parts) > 0 and parts[0].strip() else "0"
    s2 = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "0"
    match_arr = [
        {"participant_id": str(player1_id), "score_set": s1, "advancing": str(player1_id) == str(winner_id)},
        {"participant_id": str(player2_id), "score_set": s2, "advancing": str(player2_id) == str(winner_id)},
    ]
    body = {"data": {"type": "Match", "attributes": {"match": match_arr}}}
    data = await _request("PUT", f"/tournaments/{challonge_id}/matches/{match_id}.json", body=body)
    return data.get("data", data)
