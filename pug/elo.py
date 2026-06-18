from pug.config import ELO_K
from pug.storage import get_player, save_pug_data


def expected(rating: float, opp_rating: float) -> float:
    """Standard Elo expected score for a player/team rated `rating` vs `opp_rating`."""
    return 1.0 / (1.0 + 10 ** ((opp_rating - rating) / 400.0))


def _team_avg(player_ids: list[int]) -> float:
    if not player_ids:
        return 0.0
    return sum(get_player(pid)["elo"] for pid in player_ids) / len(player_ids)


def apply_match(winner_ids: list[int], loser_ids: list[int]) -> dict[int, int]:
    """Update ELO for a finished match.

    Uses team-average expected scores so the delta scales with opponent strength.
    Every player on a team receives the same (rounded) team delta. Also bumps
    win/loss counts and persists. Returns {discord_id: delta} for display.
    """
    win_avg = _team_avg(winner_ids)
    lose_avg = _team_avg(loser_ids)

    exp_win = expected(win_avg, lose_avg)   # expected score of the winning team
    win_delta = round(ELO_K * (1 - exp_win))    # winners gain this
    lose_delta = win_delta                       # losers lose the same amount (symmetric)

    deltas: dict[int, int] = {}

    for pid in winner_ids:
        player = get_player(pid)
        player["elo"] += win_delta
        player["wins"] += 1
        deltas[pid] = win_delta

    for pid in loser_ids:
        player = get_player(pid)
        player["elo"] = max(0, player["elo"] - lose_delta)
        player["losses"] += 1
        deltas[pid] = -lose_delta

    # Record an ELO history point for everyone (for the /rank graph). Cap the history
    # so the persisted blob can't grow without bound.
    for pid in list(winner_ids) + list(loser_ids):
        player = get_player(pid)
        hist = player.setdefault("elo_history", [])
        hist.append(player["elo"])
        if len(hist) > 100:
            del hist[:-100]

    save_pug_data()
    return deltas
