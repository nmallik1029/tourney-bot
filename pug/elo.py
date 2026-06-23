from pug.config import ELO_K
from pug.storage import get_player, save_pug_data

ELO_MIN_CHANGE = 3
ELO_MAX_CHANGE = 35
ELO_PERFORMANCE_MULTIPLIER = 4.0
ELO_PERFORMANCE_MAX_ADJUST = 8


def expected(rating: float, opp_rating: float) -> float:
    """Standard Elo expected score for a player/team rated `rating` vs `opp_rating`."""
    return 1.0 / (1.0 + 10 ** ((opp_rating - rating) / 400.0))


def _team_avg(player_ids: list[int]) -> float:
    if not player_ids:
        return 0.0
    return sum(get_player(pid)["elo"] for pid in player_ids) / len(player_ids)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _performance_adjustments(team_ids: list[int], player_stats: dict[int, dict] | None) -> dict[int, int]:
    """Return bounded per-player Elo adjustments based on same-team CKL rating.

    The team average is the baseline, so the result remains mostly Elo/team based:
    carries get a little extra, weaker games get a little less, and the modifier is
    capped so one stat line cannot swamp the match result.
    """
    if not team_ids or not player_stats:
        return {pid: 0 for pid in team_ids}

    from scoreboard import ckl_rating

    ratings: dict[int, float] = {}
    for pid in team_ids:
        stats = player_stats.get(pid)
        if not stats:
            return {p: 0 for p in team_ids}

        ratings[pid] = ckl_rating(
            stats.get("kills", 0),
            stats.get("deaths", 0),
            stats.get("obj", 0),
            stats.get("dmg", 0),
            stats.get("rounds", 2),
        )

    team_rating = sum(ratings.values()) / len(ratings)
    return {
        pid: _clamp(
            round((rating - team_rating) * ELO_PERFORMANCE_MULTIPLIER),
            -ELO_PERFORMANCE_MAX_ADJUST,
            ELO_PERFORMANCE_MAX_ADJUST,
        )
        for pid, rating in ratings.items()
    }


def apply_match(
    winner_ids: list[int],
    loser_ids: list[int],
    player_stats: dict[int, dict] | None = None,
) -> dict[int, int]:
    """Update ELO for a finished match.

    Uses team-average expected scores so the delta scales with opponent strength.
    When scoreboard stats are available, each player also gets a bounded
    performance modifier from their CKL rating relative to their own team.
    Also bumps win/loss counts and persists. Returns {discord_id: delta}.
    """
    win_avg = _team_avg(winner_ids)
    lose_avg = _team_avg(loser_ids)

    exp_win = expected(win_avg, lose_avg)   # expected score of the winning team
    base_delta = _clamp(round(ELO_K * (1 - exp_win)), ELO_MIN_CHANGE, ELO_MAX_CHANGE)
    winner_adjustments = _performance_adjustments(winner_ids, player_stats)
    loser_adjustments = _performance_adjustments(loser_ids, player_stats)

    deltas: dict[int, int] = {}

    for pid in winner_ids:
        player = get_player(pid)
        delta = _clamp(base_delta + winner_adjustments.get(pid, 0), ELO_MIN_CHANGE, ELO_MAX_CHANGE)
        player["elo"] += delta
        player["wins"] += 1
        deltas[pid] = delta

    for pid in loser_ids:
        player = get_player(pid)
        loss = _clamp(base_delta - loser_adjustments.get(pid, 0), ELO_MIN_CHANGE, ELO_MAX_CHANGE)
        player["elo"] = max(0, player["elo"] - loss)
        player["losses"] += 1
        deltas[pid] = -loss

    # Record an ELO history point for everyone (for the /rank graph) and update each
    # player's peak ELO. Cap the history so the persisted blob can't grow unbounded.
    for pid in list(winner_ids) + list(loser_ids):
        player = get_player(pid)
        hist = player.setdefault("elo_history", [])
        hist.append(player["elo"])
        if len(hist) > 100:
            del hist[:-100]
        player["peak_elo"] = max(player.get("peak_elo", player["elo"]), player["elo"])

    save_pug_data()
    return deltas
