import json
import os
import secrets
import time
from pathlib import Path

from pug.config import ELO_START

PUG_FILE = Path("pug_data.json")

# Reuse the same Gist the tournament side uses; PUG data lives in a separate file
# (pug_data.json) within that Gist, so Railway redeploys don't wipe ELO/bans/links.
GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
GIST_ID = os.environ.get("GITHUB_GIST_ID", "")


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def _default_data() -> dict:
    return {
        "players": {},      # discord_id(str) -> {elo, wins, losses, usernames[]}
        "noadds": {},       # discord_id(str) -> {reason, until, admin, at} (the single block)
        "mod_log": [],      # [{user, action, reason, admin, at, until}]
        "link_requests": {},  # rid -> {user_id, usernames, primary, region, review_message_id, review_channel_id}
        "queue_counter": 0,
        "config": {
            "queue_channel_id": 0,
            "queue_message_id": 0,
            "queue_vc_id": 0,
            "results_channel_id": 0,
            "account_link_channel_id": 0,
            "account_link_message_id": 0,
            "review_channel_id": 0,
            "category_id": 0,
        },
        "dashboard_token": "",
    }


def load_pug_data() -> dict:
    data = None
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=_gist_headers(),
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                gist = json.loads(resp.read().decode())
                files = gist.get("files", {})
                if "pug_data.json" in files:
                    data = json.loads(files["pug_data.json"]["content"])
                    print("[PugStorage] Loaded pug data from Gist")
        except Exception as e:
            print(f"[PugStorage] Failed to load from Gist: {e}")

    if data is None and PUG_FILE.exists():
        try:
            with open(PUG_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[PugStorage] Failed to load pug_data.json: {e}")

    if data is None:
        data = _default_data()

    # Backfill any missing top-level keys (forward-compat)
    base = _default_data()
    for k, v in base.items():
        data.setdefault(k, v)
    for k, v in base["config"].items():
        data["config"].setdefault(k, v)
    if not data.get("dashboard_token"):
        data["dashboard_token"] = secrets.token_urlsafe(16)

    # Migrate old list-format noadds -> {id: {reason, until, admin, at}}
    if isinstance(data.get("noadds"), list):
        data["noadds"] = {str(x): {"reason": "", "until": None, "admin": "", "at": None} for x in data["noadds"]}

    # Consolidate legacy bans into permanent/timed no-adds (no-add is the single block now).
    legacy = data.get("bans")
    if isinstance(legacy, list):
        legacy = {str(x): {"reason": "", "until": None, "admin": "", "at": None} for x in legacy}
    if isinstance(legacy, dict):
        for uid, info in legacy.items():
            if uid not in data["noadds"]:
                data["noadds"][uid] = {
                    "reason": (info or {}).get("reason") or "migrated from ban",
                    "until": (info or {}).get("until"),
                    "admin": (info or {}).get("admin", ""),
                    "at": (info or {}).get("at"),
                }
    data.pop("bans", None)

    return data


def save_pug_data():
    data_str = json.dumps(pug_data, indent=2, default=str)
    with open(PUG_FILE, "w") as f:
        f.write(data_str)
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            payload = json.dumps({"files": {"pug_data.json": {"content": data_str}}}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=payload,
                headers={**_gist_headers(), "Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"[PugStorage] Failed to save to Gist: {e}")


# Shared state
pug_data: dict = load_pug_data()
pug_queue: list[int] = []          # FIFO of Discord IDs currently queued (in-memory)
pug_matches: dict[str, dict] = {}  # match_key -> live match state (in-memory)
sim_players: dict[int, dict] = {}  # fake players for /simulate (never persisted)


# Player helpers
def get_player(discord_id: int) -> dict:
    """Return the player record, creating a default one at ELO_START if missing.

    Fake simulation players are served from the in-memory `sim_players` store so
    they never touch the leaderboard or persisted data.
    """
    if discord_id in sim_players:
        return sim_players[discord_id]
    key = str(discord_id)
    player = pug_data["players"].get(key)
    if player is None:
        player = {"elo": ELO_START, "wins": 0, "losses": 0, "usernames": [], "region": ""}
        pug_data["players"][key] = player
    # Backfill fields on older records
    player.setdefault("elo", ELO_START)
    player.setdefault("wins", 0)
    player.setdefault("losses", 0)
    player.setdefault("usernames", [])
    player.setdefault("region", "")
    return player


def set_region(discord_id: int, region: str):
    get_player(discord_id)["region"] = (region or "").upper()
    save_pug_data()


def get_elo(discord_id: int) -> int:
    return get_player(discord_id)["elo"]


def add_username(discord_id: int, username: str) -> list[str]:
    """Append a Krunker username to a player (case-insensitive dedupe). Returns the list."""
    player = get_player(discord_id)
    existing_lower = {u.lower() for u in player["usernames"]}
    if username.lower() not in existing_lower:
        player["usernames"].append(username)
    save_pug_data()
    return player["usernames"]


def remove_username(discord_id: int, username: str) -> list[str]:
    player = get_player(discord_id)
    player["usernames"] = [u for u in player["usernames"] if u.lower() != username.lower()]
    save_pug_data()
    return player["usernames"]


def username_to_discord(username: str) -> int | None:
    """Reverse-lookup a Krunker username to a Discord ID (case-insensitive)."""
    target = username.strip().lower()
    for did, player in pug_data["players"].items():
        for u in player.get("usernames", []):
            if u.strip().lower() == target:
                return int(did)
    return None


def primary_username(discord_id: int, fallback: str = "") -> str:
    """First linked username for a player, or a fallback."""
    names = get_player(discord_id)["usernames"]
    return names[0] if names else fallback


def set_primary_username(discord_id: int, username: str) -> bool:
    """Move a linked username to the front (its primary slot). Returns False if not linked."""
    player = get_player(discord_id)
    target = username.strip().lower()
    match = next((u for u in player["usernames"] if u.lower() == target), None)
    if not match:
        return False
    player["usernames"].remove(match)
    player["usernames"].insert(0, match)
    save_pug_data()
    return True


def set_elo(discord_id: int, elo: int):
    get_player(discord_id)["elo"] = elo
    save_pug_data()


def reset_player(discord_id: int):
    """Reset a single player's ELO to the start value and clear their W/L."""
    p = get_player(discord_id)
    p["elo"] = ELO_START
    p["wins"] = 0
    p["losses"] = 0
    save_pug_data()


def reset_profile(discord_id: int):
    """Clear a player's linked usernames and region (leaves ELO/W-L untouched)."""
    p = get_player(discord_id)
    p["usernames"] = []
    p["region"] = ""
    save_pug_data()


def reset_elo_all():
    """Reset ELO + W/L for every player."""
    for p in pug_data["players"].values():
        p["elo"] = ELO_START
        p["wins"] = 0
        p["losses"] = 0
    save_pug_data()


# Moderation helpers
def _log_mod(discord_id: int, action: str, reason: str, admin: str, until: float | None):
    pug_data.setdefault("mod_log", []).append({
        "user": str(discord_id),
        "action": action,          # ban | unban | noadd | unnoadd
        "reason": reason or "",
        "admin": admin or "",
        "at": time.time(),
        "until": until,
    })


def get_user_mod_log(discord_id: int) -> list[dict]:
    key = str(discord_id)
    return [e for e in pug_data.get("mod_log", []) if e.get("user") == key]


def _is_active(info: dict, store: dict, key: str) -> bool:
    """Shared active-check with auto-expiry for bans/no-adds."""
    if not info:
        return False
    until = info.get("until")
    if until is not None and time.time() > until:
        store.pop(key, None)
        save_pug_data()
        return False
    return True


def is_noadded(discord_id: int) -> bool:
    """True if the player is currently no-added. Auto-expires timed no-adds."""
    key = str(discord_id)
    return _is_active(pug_data["noadds"].get(key), pug_data["noadds"], key)


def get_noadd_info(discord_id: int) -> dict | None:
    """Return {reason, until, admin, at} for an active no-add, or None. Auto-expires."""
    if not is_noadded(discord_id):
        return None
    return pug_data["noadds"].get(str(discord_id))


def set_noadd(discord_id: int, noadded: bool, reason: str = "", until: float | None = None, admin: str = ""):
    """Add or remove a no-add. `until` is an epoch timestamp (None = permanent)."""
    key = str(discord_id)
    if noadded:
        pug_data["noadds"][key] = {"reason": reason or "", "until": until, "admin": admin or "", "at": time.time()}
        _log_mod(discord_id, "noadd", reason, admin, until)
    elif key in pug_data["noadds"]:
        pug_data["noadds"].pop(key, None)
        _log_mod(discord_id, "unnoadd", "", admin, None)
    save_pug_data()


def next_queue_number() -> int:
    pug_data["queue_counter"] += 1
    save_pug_data()
    return pug_data["queue_counter"]


def reset_queue_counter(value: int = 0):
    pug_data["queue_counter"] = max(0, value)
    save_pug_data()
