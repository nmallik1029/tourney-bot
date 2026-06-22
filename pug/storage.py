import json
import os
import secrets
import time
from pathlib import Path

from pug.config import ELO_START
from core.config import SERVER_ID
from core.guild_ctx import current_guild

PUG_FILE = Path("pug_data.json")

# The guild that owned all data before the bot went multi-server. Legacy (flat) data
# is migrated under this id so the original server keeps its ELO/links/bans.
DEFAULT_GUILD = SERVER_ID

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
            "flags_channel_id": 0,
            "flags_log_channel_id": 0,
            "bigboard_channel_id": 0,
            "bigboard_message_id": 0,
            "bigboard_stat": "elo",
            "bigboard_page": 0,
        },
        "dashboard_token": "",
    }


def _backfill_guild(data: dict) -> dict:
    """Bring one guild's data block up to the current shape (forward-compat + legacy
    migrations). Mutates and returns it."""
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


def _looks_flat(raw: dict) -> bool:
    """Old single-server file had data keys (players/config/...) at the top level.
    The new multi-server file is keyed by guild id, so a top-level 'players' means
    we're looking at legacy data that needs wrapping under DEFAULT_GUILD."""
    return isinstance(raw, dict) and ("players" in raw or "config" in raw)


def load_pug_store() -> dict:
    """Load the whole multi-guild store: {guild_id(str): per_guild_data}. Migrates a
    legacy flat file by nesting it under the original server's id."""
    raw = None
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
                    raw = json.loads(files["pug_data.json"]["content"])
                    print("[PugStorage] Loaded pug data from Gist")
        except Exception as e:
            print(f"[PugStorage] Failed to load from Gist: {e}")

    if raw is None and PUG_FILE.exists():
        try:
            with open(PUG_FILE, "r") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[PugStorage] Failed to load pug_data.json: {e}")

    if raw is None:
        store = {}
    elif _looks_flat(raw):
        print(f"[PugStorage] Migrating legacy flat pug data under guild {DEFAULT_GUILD}")
        store = {str(DEFAULT_GUILD): raw}
    else:
        store = raw

    for gid, gd in store.items():
        _backfill_guild(gd)
    return store


def save_pug_data():
    data_str = json.dumps(pug_store, indent=2, default=str)
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


# ── Multi-guild state ────────────────────────────────────────────────────────────
# Persisted, keyed by guild id (str). In-memory live state is keyed by guild id (int).
pug_store: dict[str, dict] = load_pug_store()
_queues: dict[int, list[int]] = {}      # guild_id -> FIFO of queued Discord ids
_matches: dict[int, dict[str, dict]] = {}  # guild_id -> {match_key: live match}
_sims: dict[int, dict[int, dict]] = {}  # guild_id -> {fake_id: fake player}


def gdata(guild_id: int | None = None) -> dict:
    """The persisted data block for a guild (defaults to the current context), created
    and backfilled on first access."""
    gid = current_guild() if guild_id is None else int(guild_id)
    key = str(gid)
    gd = pug_store.get(key)
    if gd is None:
        gd = _backfill_guild(_default_data())
        pug_store[key] = gd
    return gd


def queue_for(guild_id: int | None = None) -> list[int]:
    gid = current_guild() if guild_id is None else int(guild_id)
    return _queues.setdefault(gid, [])


def matches_for(guild_id: int | None = None) -> dict[str, dict]:
    gid = current_guild() if guild_id is None else int(guild_id)
    return _matches.setdefault(gid, {})


def sims_for(guild_id: int | None = None) -> dict[int, dict]:
    gid = current_guild() if guild_id is None else int(guild_id)
    return _sims.setdefault(gid, {})


def all_guild_ids() -> list[int]:
    """Every guild that has persisted data or live state."""
    ids = {int(g) for g in pug_store}
    ids |= set(_queues) | set(_matches)
    return list(ids)


def iter_all_matches():
    """Yield (guild_id, match) for every live match across all guilds (for the webhook,
    which has no ambient guild until it identifies the match)."""
    for gid, table in _matches.items():
        for m in list(table.values()):
            yield gid, m


def clear_all_queues():
    """Wipe every guild's in-memory queue (used on restart)."""
    _queues.clear()


def guild_for_dashboard_token(token: str) -> int | None:
    """Find which guild a pug-dashboard token belongs to (tokens are per-guild now)."""
    if not token:
        return None
    for gid, gd in pug_store.items():
        if gd.get("dashboard_token") == token:
            return int(gid)
    return None


class _GuildDictProxy:
    """Dict-like view of the current guild's persisted data, so existing call sites
    (`pug_data["players"]`, `pug_data.get(...)`, ...) keep working unchanged."""

    def _d(self) -> dict:
        return gdata()

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def __contains__(self, k):
        return k in self._d()

    def get(self, k, default=None):
        return self._d().get(k, default)

    def setdefault(self, k, default=None):
        return self._d().setdefault(k, default)

    def pop(self, k, *args):
        return self._d().pop(k, *args)

    def keys(self):
        return self._d().keys()

    def values(self):
        return self._d().values()

    def items(self):
        return self._d().items()


class _GuildListProxy:
    """List-like view of the current guild's in-memory queue."""

    def _l(self) -> list:
        return queue_for()

    def __len__(self):
        return len(self._l())

    def __iter__(self):
        return iter(self._l())

    def __contains__(self, x):
        return x in self._l()

    def __getitem__(self, i):
        return self._l()[i]

    def __bool__(self):
        return bool(self._l())

    def append(self, x):
        self._l().append(x)

    def remove(self, x):
        self._l().remove(x)

    def insert(self, i, x):
        self._l().insert(i, x)

    def pop(self, i=-1):
        return self._l().pop(i)

    def index(self, x):
        return self._l().index(x)

    def clear(self):
        self._l().clear()


class _GuildMatchProxy:
    """Dict-like view of the current guild's live matches (keyed by match key)."""

    def _m(self) -> dict:
        return matches_for()

    def __getitem__(self, k):
        return self._m()[k]

    def __setitem__(self, k, v):
        self._m()[k] = v

    def __contains__(self, k):
        return k in self._m()

    def get(self, k, default=None):
        return self._m().get(k, default)

    def pop(self, k, *args):
        return self._m().pop(k, *args)

    def values(self):
        return self._m().values()

    def items(self):
        return self._m().items()

    def keys(self):
        return self._m().keys()


class _GuildSimProxy:
    """Dict-like view of the current guild's simulation players."""

    def _s(self) -> dict:
        return sims_for()

    def __getitem__(self, k):
        return self._s()[k]

    def __setitem__(self, k, v):
        self._s()[k] = v

    def __contains__(self, k):
        return k in self._s()

    def get(self, k, default=None):
        return self._s().get(k, default)

    def clear(self):
        self._s().clear()


# Proxies resolve to the guild bound in the current context. Existing imports
# (`from pug.storage import pug_data, pug_queue, pug_matches, sim_players`) are unchanged.
pug_data = _GuildDictProxy()
pug_queue = _GuildListProxy()
pug_matches = _GuildMatchProxy()
sim_players = _GuildSimProxy()


def in_active_match(discord_id: int) -> dict | None:
    """Return the live match a user is involved in (any phase/role), else None.

    Checks every place a real player can appear so someone in a game can never
    sneak back into the queue: the roster, captains, drafted teams, and the
    simulation controller.
    """
    for m in pug_matches.values():
        if discord_id in m.get("players", []):
            return m
        if discord_id in m.get("captains", []):
            return m
        if m.get("sim_controller") == discord_id:
            return m
        for roster in m.get("teams", {}).values():
            if discord_id in roster:
                return m
    return None


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
    # Per-game stat accumulators + flag counters + ELO history (for /rank).
    player.setdefault("kills", 0)
    player.setdefault("deaths", 0)
    player.setdefault("obj", 0)
    player.setdefault("dmg", 0)
    player.setdefault("games", 0)
    player.setdefault("rating_sum", 0.0)   # cumulative CKL rating, for the average
    # Rating gets its own game counter so it can be reset independently of K/D / OBJ.
    # Backfill from `games` for existing players (every recorded game added to both).
    player.setdefault("rating_games", player.get("games", 0))
    player.setdefault("elo_history", [])
    player.setdefault("peak_elo", player.get("elo", ELO_START))
    player.setdefault("low_kd_flags", 0)
    player.setdefault("low_obj_flags", 0)
    return player


def record_match_stats(discord_id: int, kills: int, deaths: int, obj: int, dmg: int, rounds: int = 2):
    """Add one game's kills/deaths/obj/dmg to a player's cumulative totals and add the
    game's CKL rating (round-normalized) to their rating sum. Does NOT save (the caller
    batches the save)."""
    from scoreboard import ckl_rating
    p = get_player(discord_id)
    p["kills"] += int(kills)
    p["deaths"] += int(deaths)
    p["obj"] += int(obj)
    p["dmg"] = p.get("dmg", 0) + int(dmg)
    p["games"] += 1
    p["rating_sum"] = p.get("rating_sum", 0.0) + ckl_rating(int(kills), int(deaths), int(obj), int(dmg), rounds)
    p["rating_games"] = p.get("rating_games", 0) + 1


def get_avg_rating(discord_id: int) -> float:
    """A player's average CKL rating across their rated games (0 if none)."""
    p = get_player(discord_id)
    g = p.get("rating_games", 0)
    return round(p.get("rating_sum", 0.0) / g, 2) if g else 0.0


def reset_rating(discord_id: int):
    """Wipe a player's average CKL rating (rating sum + rated-game count) without
    touching their ELO, W/L, K/D, or OBJ. Their average rebuilds from future games."""
    p = get_player(discord_id)
    p["rating_sum"] = 0.0
    p["rating_games"] = 0
    save_pug_data()


def top_rated_players(limit: int = 5) -> list:
    """Return [(discord_id, avg_rating, rated_games)] for players with >=1 rated game."""
    out = []
    for did_s, p in pug_data["players"].items():
        g = p.get("rating_games", 0)
        if g > 0:
            out.append((int(did_s), round(p.get("rating_sum", 0.0) / g, 2), g))
    out.sort(key=lambda t: t[1], reverse=True)
    return out[:limit]


def add_flag(discord_id: int, reason: str) -> int:
    """Increment a player's flag counter for 'kd' or 'obj' and return the new count."""
    p = get_player(discord_id)
    field = "low_kd_flags" if reason == "kd" else "low_obj_flags"
    p[field] = p.get(field, 0) + 1
    save_pug_data()
    return p[field]


def get_flag_count(discord_id: int, reason: str) -> int:
    p = get_player(discord_id)
    return p.get("low_kd_flags" if reason == "kd" else "low_obj_flags", 0)


def remove_flag(discord_id: int, reason: str, amount: int = 1) -> int:
    """Decrement a player's flag counter for 'kd' or 'obj' (floored at 0). Pass a large
    amount (or use clear_flags) to wipe them. Returns the new count."""
    p = get_player(discord_id)
    field = "low_kd_flags" if reason == "kd" else "low_obj_flags"
    p[field] = max(0, p.get(field, 0) - amount)
    save_pug_data()
    return p[field]


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


def set_username(discord_id: int, username: str):
    """Set a player's single linked Krunker account, replacing any existing one(s)."""
    get_player(discord_id)["usernames"] = [username]
    save_pug_data()


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
