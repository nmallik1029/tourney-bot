import json
import os
import aiohttp

CHALLONGE_API_KEY = os.environ.get("CHALLONGE_API_KEY", "")
CHALLONGE_BASE = "https://api.challonge.com/v1"


async def challonge_request(method: str, path: str, **kwargs) -> dict | list:
    url = f"{CHALLONGE_BASE}{path}"
    params = kwargs.pop("params", {})
    params["api_key"] = CHALLONGE_API_KEY
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, params=params, **kwargs) as resp:
            text = await resp.text()
            if resp.status >= 400:
                print(f"[Challonge] {method} {path} -> {resp.status}: {text[:500]}")
                raise Exception(f"{resp.status}, {text[:200]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                raise Exception(f"Non-JSON response: {text[:200]}")


async def challonge_create_tournament(name: str, url_slug: str) -> dict:
    data = await challonge_request("POST", "/tournaments.json", data={
        "tournament[name]": name,
        "tournament[url]": url_slug,
        "tournament[tournament_type]": "double elimination",
        "tournament[private]": "false",
        "tournament[game_name]": "Krunker",
        "tournament[subdomain]": "krunker-biweeklies",
    })
    return data.get("tournament", data)


async def challonge_add_participants(challonge_id, teams_with_seeds: list[dict]) -> list:
    """teams_with_seeds: [{"name": "...", "seed": 1, "misc": "team_id"}, ...]"""
    added = []
    for t in teams_with_seeds:
        form = {
            "participant[name]": t["name"],
            "participant[seed]": str(t["seed"]),
        }
        if t.get("misc"):
            form["participant[misc]"] = t["misc"]
        data = await challonge_request("POST", f"/tournaments/{challonge_id}/participants.json", data=form)
        added.append(data)
    return added


async def challonge_start(challonge_id) -> dict:
    data = await challonge_request("POST", f"/tournaments/{challonge_id}/start.json", params={
        "include_matches": 1, "include_participants": 1,
    })
    return data.get("tournament", data)


async def challonge_get_matches(challonge_id, state="open") -> list:
    data = await challonge_request("GET", f"/tournaments/{challonge_id}/matches.json", params={
        "state": state,
    })
    return [m["match"] for m in data] if isinstance(data, list) else []


async def challonge_get_participants(challonge_id) -> list:
    data = await challonge_request("GET", f"/tournaments/{challonge_id}/participants.json")
    return [p["participant"] for p in data] if isinstance(data, list) else []


async def challonge_update_match(challonge_id, match_id, scores_csv: str, winner_id: int) -> dict:
    data = await challonge_request("PUT", f"/tournaments/{challonge_id}/matches/{match_id}.json", data={
        "match[scores_csv]": scores_csv,
        "match[winner_id]": str(winner_id),
    })
    return data.get("match", data)
