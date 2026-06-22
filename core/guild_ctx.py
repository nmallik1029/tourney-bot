"""Per-guild execution context.

The bot now serves multiple Discord servers, and their data must stay isolated.
Rather than thread a `guild_id` argument through every storage call, we keep the
"current guild" in a `ContextVar` that is set at each entry point:

  - slash commands  -> set in the command tree's interaction_check
  - button/select/modal clicks -> set in GuildView.interaction_check
  - gateway listeners (on_message/on_voice) -> set from the event's guild
  - the Krunker webhook -> set from the match the result belongs to
  - background tasks -> inherit the context of the task that spawned them
    (asyncio copies the ContextVar set when create_task is called), and the
    match-driven code re-asserts it from `match["guild_id"]` to be safe.

Accessors in core/storage.py and pug/storage.py read `current_guild()` to pick
the right slice of data. If nothing set the context, they raise loudly instead
of silently touching the wrong server's data.
"""

import contextvars
from contextlib import contextmanager

_current_guild: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_guild_id", default=None
)


def set_guild(guild_id: int | None) -> None:
    """Bind the current guild for everything that runs after this in the same context."""
    _current_guild.set(int(guild_id) if guild_id is not None else None)


def current_guild() -> int:
    """The guild bound to the running context. Raises if none was set -- that means an
    entry point forgot to bind one, which we want to surface, not paper over."""
    gid = _current_guild.get()
    if gid is None:
        raise RuntimeError(
            "No guild bound to the current context. An entry point (command, view, "
            "listener, or webhook) must call set_guild() before touching per-guild data."
        )
    return gid


def current_guild_or_none() -> int | None:
    """Like current_guild() but returns None instead of raising (for optional paths)."""
    return _current_guild.get()


@contextmanager
def guild_context(guild_id: int):
    """Temporarily bind a guild, restoring the previous one on exit.

    Used by match-driven code (which knows its guild via match["guild_id"]) and the
    webhook, so they don't depend on an ambient binding being present/correct.
    """
    token = _current_guild.set(int(guild_id))
    try:
        yield
    finally:
        _current_guild.reset(token)
