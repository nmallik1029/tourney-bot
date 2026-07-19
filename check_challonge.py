"""Standalone Challonge credential/health check.

Reuses the bot's own services/challonge.py, so it exercises the EXACT same auth,
headers, and request code the live bot uses. If this passes with a given set of
credentials in the environment, the bot will work with them too -- no redeploy needed.

What it does (and cleans up after itself):
  1. Confirms which auth mode is configured (OAuth Connect vs. v1 API key).
  2. Fetches an OAuth token (OAuth mode only).
  3. Creates a throwaway tournament, adds 2 teams, starts it, reports one result,
     then DELETES the tournament.

Run it with the same env your bot uses. For the local test creds:
    check_challonge.bat
Or load whatever creds you want to verify into the shell first, then: python check_challonge.py
"""

import asyncio
import io
import os
import sys
import time

# Load .env.test if present so `python check_challonge.py` works without the .bat.
# setdefault means creds already exported in the shell (e.g. prod OAuth) win.
if os.path.exists(".env.test"):
    for _line in io.open(".env.test", encoding="utf-8"):
        _line = _line.strip()
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k, _v)

import services.challonge as ch  # noqa: E402


def _fail(msg: str):
    print(f"\nFAIL: {msg}")
    sys.exit(1)


async def main():
    print("=== Challonge credential check ===\n")

    # 1. Which auth mode?
    if ch.API_KEY:
        print("[1/6] Auth mode: v1 API key (CHALLONGE_API_KEY set)")
    elif ch.CLIENT_ID and ch.CLIENT_SECRET:
        print("[1/6] Auth mode: OAuth Connect (CHALLONGE_CLIENT_ID/SECRET set)")
    else:
        _fail("No credentials in environment. Set CHALLONGE_API_KEY, or "
              "CHALLONGE_CLIENT_ID + CHALLONGE_CLIENT_SECRET.")

    # 2. Token fetch (OAuth only)
    if not ch.API_KEY:
        try:
            token = await ch._get_token()
            print(f"[2/6] OAuth token acquired (len={len(token)}).")
        except Exception as e:
            _fail(f"OAuth token fetch failed: {e}")
    else:
        print("[2/6] Skipping token fetch (API-key mode).")

    slug = f"zzz_cred_check_{int(time.time())}"
    challonge_id = None
    try:
        # 3. Create
        t = await ch.challonge_create_tournament("zzz credential check (safe to delete)", slug)
        challonge_id = t["id"]
        print(f"[3/6] Created throwaway tournament id={challonge_id} url={t.get('full_challonge_url')}")

        # 4. Add participants
        added = await ch.challonge_add_participants(
            challonge_id,
            [{"name": "Test Team A", "seed": 1, "misc": "A"},
             {"name": "Test Team B", "seed": 2, "misc": "B"}],
        )
        pmap = {}
        for entry in added:
            p = entry.get("participant", entry)
            if p.get("misc"):
                pmap[p["misc"]] = p["id"]
        print(f"[4/6] Added {len(added)} participants; misc->id map: {pmap}")

        # 5. Start + read the open match
        await ch.challonge_start(challonge_id)
        matches = await ch.challonge_get_matches(challonge_id, state="open")
        if not matches:
            _fail("Tournament started but no open match came back.")
        # Pick an open match that actually has both participants resolved. This is
        # exactly what the bot needs to work -- if player1_id/player2_id are None
        # here, the bot can't map a Challonge match back to our teams.
        m = next((x for x in matches if x.get("player1_id") and x.get("player2_id")), None)
        if not m:
            _fail("Open match has no resolvable players (player1_id/player2_id are "
                  "None). This is the bug that breaks starting matches in the bot.")
        print(f"[5/6] Started; open match {m['id']} "
              f"(p1={m.get('player1_id')} p2={m.get('player2_id')})")

        # 6. Report a result (write scope)
        winner = m.get("player1_id")
        await ch.challonge_update_match(
            challonge_id, m["id"], m["player1_id"], m["player2_id"], "1-0", winner,
        )
        print(f"[6/6] Reported 1-0 (winner={winner}). Write scope OK.")

    except Exception as e:
        _fail(f"{e}")
    finally:
        # Always clean up the throwaway tournament. An underway tournament can't be
        # deleted directly -- revert it to pending first.
        if challonge_id:
            try:
                revert = {"data": {"type": "TournamentState", "attributes": {"state": "reset"}}}
                await ch._request("PUT", f"/tournaments/{challonge_id}/change_state.json", body=revert)
                await ch._request("DELETE", f"/tournaments/{challonge_id}.json")
                print(f"\nCleaned up throwaway tournament {challonge_id}.")
            except Exception as e:
                print(f"\nWARNING: Could not auto-delete tournament {challonge_id}: {e}")
                print("   Delete it manually on Challonge if it lingers.")

    print("\nPASS: credentials work end-to-end (auth + read + write).")


if __name__ == "__main__":
    asyncio.run(main())
