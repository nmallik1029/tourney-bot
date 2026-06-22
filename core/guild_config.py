"""Per-guild configuration: role ids and category ids, keyed by guild.

Before the bot went multi-server these lived in single global env vars (ROLE_ID,
CASTER_ROLE_ID, PUG_ADMIN_ROLE_ID, ...) and a flat config.json (the two category ids).
That can't describe two servers at once, so configuration is now per-guild, persisted
in config.json as {guild_id: {...}} and set via setup commands.

The original server's block is seeded from the existing env vars on first load, so it
keeps working with no manual setup. New servers start blank (only Discord's
Administrator permission works) until an admin runs the setup commands.

This module reads its env seeds directly from os.environ (not from core.config /
pug.config) to avoid import cycles -- the permission checks in those modules import
*this* module.
"""

import json
import os
from pathlib import Path

from core.guild_ctx import current_guild

CONFIG_FILE = Path("config.json")

_SERVER_ID = int(os.environ.get("SERVER_ID", 1481881771736956938))
GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
GIST_ID = os.environ.get("GITHUB_GIST_ID", "")


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github.v3+json"}

# Field name -> env var it was previously read from (used to seed the original server).
_ENV_SEED = {
    "mod_role_id": ("ROLE_ID", 1492368556627591248),
    "caster_role_id": ("CASTER_ROLE_ID", 0),
    "pug_admin_role_id": ("PUG_ADMIN_ROLE_ID", 0),
    "pug_helper_role_id": ("PUG_HELPER_ROLE_ID", 0),
    "pug_na_role_id": ("PUG_NA_ROLE_ID", 0),
    "pug_eu_role_id": ("PUG_EU_ROLE_ID", 0),
    "pug_captain_role_id": ("PUG_CAPTAIN_ROLE_ID", 0),
    "pug_spectator_role_id": ("PUG_SPECTATOR_ROLE_ID", 0),
}

# Every field a guild config can hold (role + category ids). Missing -> 0 (unset).
_FIELDS = list(_ENV_SEED) + ["tournament_category_id", "admin_category_id"]


def _default_block() -> dict:
    return {f: 0 for f in _FIELDS}


def _seed_from_env() -> dict:
    block = _default_block()
    for field, (env, default) in _ENV_SEED.items():
        block[field] = int(os.environ.get(env, default))
    return block


def _looks_flat(raw: dict) -> bool:
    """Legacy config.json had category ids at the top level (no guild keys)."""
    return isinstance(raw, dict) and (
        "tournament_category_id" in raw or "admin_category_id" in raw
    )


def _load() -> dict:
    raw = None
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
                files = gist.get("files", {})
                if "config.json" in files:
                    raw = json.loads(files["config.json"]["content"])
                    loaded = True
                    print("[GuildConfig] Loaded config.json from Gist")
        except Exception as e:
            print(f"[GuildConfig] Failed to load config.json from Gist: {e}")

    if not loaded and CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[GuildConfig] Failed to read config.json: {e}")

    if not raw:
        # No config yet: seed the original server from env so it keeps working.
        store = {str(_SERVER_ID): _seed_from_env()}
    elif _looks_flat(raw):
        print(f"[GuildConfig] Migrating legacy config.json under guild {_SERVER_ID}")
        block = _seed_from_env()
        block["tournament_category_id"] = int(raw.get("tournament_category_id") or 0)
        block["admin_category_id"] = int(raw.get("admin_category_id") or 0)
        store = {str(_SERVER_ID): block}
    else:
        store = raw

    # Make sure the original server always has a seeded block, and backfill fields.
    store.setdefault(str(_SERVER_ID), _seed_from_env())
    for block in store.values():
        for f in _FIELDS:
            block.setdefault(f, 0)
    return store


config_store: dict[str, dict] = _load()


def save_guild_config():
    data_str = json.dumps(config_store, indent=2)
    with open(CONFIG_FILE, "w") as f:
        f.write(data_str)
    if GIST_TOKEN and GIST_ID:
        try:
            import urllib.request
            payload = json.dumps({"files": {"config.json": {"content": data_str}}}).encode()
            req = urllib.request.Request(
                f"https://api.github.com/gists/{GIST_ID}",
                data=payload,
                headers={**_gist_headers(), "Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as e:
            print(f"[GuildConfig] Failed to save config.json to Gist: {e}")


def gconf(guild_id: int | None = None) -> dict:
    """The config block for a guild (defaults to the current context), created on demand."""
    gid = current_guild() if guild_id is None else int(guild_id)
    key = str(gid)
    block = config_store.get(key)
    if block is None:
        block = _default_block()
        config_store[key] = block
    return block


def get_id(field: str, guild_id: int | None = None) -> int:
    return int(gconf(guild_id).get(field, 0) or 0)


def set_id(field: str, value: int, guild_id: int | None = None):
    gconf(guild_id)[field] = int(value or 0)
    save_guild_config()


# Convenience accessors used by the permission checks / role logic.
def mod_role_id(guild_id: int | None = None) -> int:
    return get_id("mod_role_id", guild_id)


def caster_role_id(guild_id: int | None = None) -> int:
    return get_id("caster_role_id", guild_id)


def pug_admin_role_id(guild_id: int | None = None) -> int:
    return get_id("pug_admin_role_id", guild_id)


def pug_helper_role_id(guild_id: int | None = None) -> int:
    return get_id("pug_helper_role_id", guild_id)


def pug_na_role_id(guild_id: int | None = None) -> int:
    return get_id("pug_na_role_id", guild_id)


def pug_eu_role_id(guild_id: int | None = None) -> int:
    return get_id("pug_eu_role_id", guild_id)


def pug_captain_role_id(guild_id: int | None = None) -> int:
    return get_id("pug_captain_role_id", guild_id)


def pug_spectator_role_id(guild_id: int | None = None) -> int:
    return get_id("pug_spectator_role_id", guild_id)


def tournament_category_id(guild_id: int | None = None) -> int:
    return get_id("tournament_category_id", guild_id)


def admin_category_id(guild_id: int | None = None) -> int:
    return get_id("admin_category_id", guild_id)


# Challonge Community permalink / id (e.g. "myleague" from
# challonge.com/communities/myleague). Empty -> tournaments are filed under the
# account's main page. The Community must be created manually on challonge.com; the
# API can't create it.
def challonge_subdomain(guild_id: int | None = None) -> str:
    return str(gconf(guild_id).get("challonge_subdomain", "") or "").strip()


def set_challonge_subdomain(value: str, guild_id: int | None = None):
    gconf(guild_id)["challonge_subdomain"] = (value or "").strip()
    save_guild_config()
