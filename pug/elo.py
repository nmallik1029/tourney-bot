from pug.storage import get_player, save_pug_data

ELO_MIN_WIN_CHANGE = 1
ELO_MIN_LOSS_CHANGE = 1
ELO_MAX_CHANGE = 100
# How much of a favored player's "extra" loss (the part beyond an even-match loss) to keep.
# Standard Elo makes a high-rated player drop ~ELO_MAX_CHANGE*exp on a loss, which is brutal
# when they're heavily favored. 0.5 halves that excess so high-Elo losses sting less; 1.0
# would be standard Elo, 0.0 would cap every favored loss at the even-match value.
ELO_FAVORED_LOSS_KEEP = 0.5
ELO_PERFORMANCE_MAX_ADJUST = 30
ELO_RATING_EXPECTATION_STEP = 250.0
ELO_PERFORMANCE_POSITIVE_MULTIPLIER = 12.0
ELO_PERFORMANCE_NEGATIVE_MULTIPLIER = 8.0
ELO_PERFORMANCE_NEGATIVE_GRACE = 1.5


def expected(rating: float, opp_rating: float) -> float:
    """Standard Elo expected score for a player/team rated `rating` vs `opp_rating`."""
    return 1.0 / (1.0 + 10 ** ((opp_rating - rating) / 400.0))


def _team_avg(player_ids: list[int]) -> float:
    if not player_ids:
        return 0.0
    return sum(get_player(pid)["elo"] for pid in player_ids) / len(player_ids)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _base_result_delta(pid: int, opponent_avg: float, won: bool) -> int:
    """Per-player result delta from current Elo distribution, before performance."""
    exp = expected(get_player(pid)["elo"], opponent_avg)
    if won:
        return _clamp(round(ELO_MAX_CHANGE * (1.0 - exp)), ELO_MIN_WIN_CHANGE, ELO_MAX_CHANGE)
    # Standard Elo loss is ELO_MAX_CHANGE * exp. For a favored player (exp > 0.5) we soften
    # only the "extra" loss above an even-match loss, so being highly rated no longer means a
    # near-full drop when you lose. Underdog losses (exp <= 0.5) are left untouched.
    loss = ELO_MAX_CHANGE * exp
    even = ELO_MAX_CHANGE * 0.5
    if loss > even:
        loss = even + (loss - even) * ELO_FAVORED_LOSS_KEEP
    return -_clamp(round(loss), ELO_MIN_LOSS_CHANGE, ELO_MAX_CHANGE)


def _performance_adjustments(
    player_ids: list[int],
    player_stats: dict[int, dict] | None,
) -> dict[int, int]:
    """Return bounded per-player Elo bonuses from CKL rating adjusted for current Elo.

    Higher-Elo players are expected to produce stronger CKL ratings, so a 7.8 from a
    1500-Elo player earns less bonus than the same line from a lower-rated player in
    the same lobby. Negative bonuses are intentionally forgiving and mostly reserved
    for hard-carry stat lines.
    """
    if not player_ids or not player_stats:
        return {pid: 0 for pid in player_ids}

    from scoreboard import ckl_rating

    ratings: dict[int, float] = {}
    for pid in player_ids:
        stats = player_stats.get(pid)
        if not stats:
            return {p: 0 for p in player_ids}

        ratings[pid] = ckl_rating(
            stats.get("kills", 0),
            stats.get("deaths", 0),
            stats.get("obj", 0),
            stats.get("dmg", 0),
            stats.get("rounds", 2),
        )

    lobby_rating = sum(ratings.values()) / len(ratings)
    lobby_elo = sum(get_player(pid)["elo"] for pid in player_ids) / len(player_ids)

    adjustments: dict[int, int] = {}
    for pid, rating in ratings.items():
        stats = player_stats.get(pid, {})
        elo = get_player(pid)["elo"]
        expected_rating = lobby_rating + ((elo - lobby_elo) / ELO_RATING_EXPECTATION_STEP)
        over_expected = rating - expected_rating

        if over_expected >= 0:
            bonus = round(over_expected * ELO_PERFORMANCE_POSITIVE_MULTIPLIER)
        elif over_expected <= -ELO_PERFORMANCE_NEGATIVE_GRACE:
            bonus = round((over_expected + ELO_PERFORMANCE_NEGATIVE_GRACE) * ELO_PERFORMANCE_NEGATIVE_MULTIPLIER)
        else:
            bonus = 0

        kills = int(stats.get("kills", 0) or 0)
        deaths = int(stats.get("deaths", 0) or 0)
        hard_carried = deaths >= max(6, kills * 3) and deaths - kills >= 8
        if bonus < 0 and not hard_carried:
            bonus = 0

        adjustments[pid] = _clamp(bonus, -ELO_PERFORMANCE_MAX_ADJUST, ELO_PERFORMANCE_MAX_ADJUST)

    return adjustments


def apply_match(
    winner_ids: list[int],
    loser_ids: list[int],
    player_stats: dict[int, dict] | None = None,
) -> dict[int, dict]:
    """Update ELO for a finished match.

    Uses per-player expected scores against the opposing team's average Elo so the
    base delta scales from -100 to +100 with the current lobby distribution.
    When scoreboard stats are available, each player also gets a bounded
    performance modifier from their CKL rating relative to lobby rating and their
    own Elo. Also bumps win/loss counts and persists.

    Returns {discord_id: {"base": int, "bonus": int, "delta": int}}.
    """
    win_opp_avg = _team_avg(loser_ids)
    lose_opp_avg = _team_avg(winner_ids)
    all_ids = list(winner_ids) + list(loser_ids)
    adjustments = _performance_adjustments(all_ids, player_stats)

    deltas: dict[int, dict] = {}

    for pid in winner_ids:
        player = get_player(pid)
        base = _base_result_delta(pid, win_opp_avg, won=True)
        bonus = adjustments.get(pid, 0)
        delta = _clamp(base + bonus, ELO_MIN_WIN_CHANGE, ELO_MAX_CHANGE)
        player["elo"] += delta
        player["wins"] += 1
        deltas[pid] = {"base": base, "bonus": bonus, "delta": delta}

    for pid in loser_ids:
        player = get_player(pid)
        base = _base_result_delta(pid, lose_opp_avg, won=False)
        bonus = adjustments.get(pid, 0)
        delta = _clamp(base + bonus, -ELO_MAX_CHANGE, -ELO_MIN_LOSS_CHANGE)
        player["elo"] = max(0, player["elo"] + delta)
        player["losses"] += 1
        deltas[pid] = {"base": base, "bonus": bonus, "delta": delta}

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
