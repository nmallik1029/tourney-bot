import json
import os
from pathlib import Path

from core.config import SERVER_ID

GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
GIST_ID = os.environ.get("GITHUB_GIST_ID", "")
TOURNAMENTS_FILE = Path("tournaments.json")


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def load_tournaments() -> dict:
    """Load tournaments (a flat {tournament_id: {...}} map; ids are globally unique).
    Tournaments are isolated per server by a `guild_id` stamp + guild-filtered listings,
    not by nesting, so the web admin layer can keep looking them up by id directly.
    Legacy tournaments with no stamp are attributed to the original server."""
    data = {}
    loaded = False
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                gist = json.loads(resp.read().decode())
                content = gist["files"]["tournaments.json"]["content"]
                data = json.loads(content)
                loaded = True
                print(f"[Storage] Loaded {len(data)} tournaments from Gist")
        except Exception as e:
            print(f"[Storage] Failed to load from Gist: {e}")
    if not loaded and TOURNAMENTS_FILE.exists():
        with open(TOURNAMENTS_FILE, "r") as f:
            data = json.load(f)

    for t in data.values():
        if not t.get("guild_id"):
            t["guild_id"] = SERVER_ID
    return data


def tournaments_for_guild(guild_id: int) -> dict:
    """Only the tournaments belonging to one server (for listings / view re-registration)."""
    return {tid: t for tid, t in tournaments.items() if t.get("guild_id") == guild_id}


def find_tournament(tournament_id: str):
    """Look a tournament up by its (globally unique) id, ignoring guild. Returns the
    tournament dict or None. Used by the web admin layer, which has no guild context."""
    return tournaments.get(tournament_id)


def save_tournaments():
    data_str = json.dumps(tournaments, indent=2, default=str)
    with open(TOURNAMENTS_FILE, "w") as f:
        f.write(data_str)
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            payload = json.dumps({"files": {"tournaments.json": {"content": data_str}}}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=payload,
                headers={**_gist_headers(), "Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"[Storage] Failed to save to Gist: {e}")


def load_config() -> dict:
    config_file = Path("config.json")
    if config_file.exists():
        with open(config_file, "r") as f:
            return json.load(f)
    return {}


def save_config(data: dict):
    with open("config.json", "w") as f:
        json.dump(data, f, indent=2)


# Shared state
tournaments: dict[str, dict] = load_tournaments()
active_matches: dict[str, dict] = {}
pending_registrations: dict[int, dict] = {}
# Tournament dashboard tokens, mapped to the guild they're scoped to so the dashboard
# only ever lists that server's tournaments. In-memory (reset on restart).
_dashboard_tokens: dict[str, int] = {}
