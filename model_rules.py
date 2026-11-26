"""Règles de sélection partagées par le bot et son backtest."""


TEAM_SOT_MAX_MEAN = 12
TEAM_SOT_MIN_PROBABILITY = 0.65
BZZOIRO_TEAM_SOT_MIN_SAMPLES = 8
PLAYER_SOT_MIN_SAMPLES = 8
PLAYER_SOT_MIN_AVG_MINUTES = 75
PLAYER_SOT_MIN_HIT_RATE = 0.50
PLAYER_OVER_05_MIN_PROBABILITY = 0.70
PLAYER_OVER_05_MIN_HIT_RATE = 0.55
PLAYER_OVER_15_MIN_PROBABILITY = 0.65
PLAYER_OVER_15_MIN_HIT_RATE = 0.40


def is_confirmed_lineup(availability):
    """True uniquement si l'API confirme la composition du match cible."""
    if not isinstance(availability, dict):
        return False
    available = availability.get("available", {})
    return bool(
        isinstance(available, dict)
        and available.get("lineups")
        and availability.get("lineup_status") == "confirmed"
    )


def normalized_player_position(profile):
    if not isinstance(profile, dict):
        return ""
    value = profile.get("position", profile.get("position_name", profile.get("role", "")))
    return " ".join(str(value).lower().replace("_", " ").split())


def is_attacking_player(position):
    position = normalized_player_position({"position": position})
    if not position:
        return False
    defensive_tokens = (
        "goalkeeper", "goal keeper", "keeper", "defender", "centre back",
        "center back", "centre-back", "center-back", "full back", "full-back",
        "left back", "right back", "wing back", "sweeper", "goalie",
    )
    if any(token in position for token in defensive_tokens):
        return False
    defensive_codes = {"gk", "gb", "df", "d", "cb", "dc", "lb", "rb", "lwb", "rwb", "dl", "dr"}
    if position in defensive_codes:
        return False
    attacking_tokens = (
        "forward", "striker", "attacker", "winger", "midfielder", "midfield",
        "second striker", "offensive", "am", "wing",
    )
    return position in {"f", "fw", "st", "cf", "lf", "rf", "m", "mf", "am"} or any(
        token in position for token in attacking_tokens
    )


def select_team_sot(profile, line_probabilities, min_samples=0):
    """Retourne la meilleure sélection équipe; le minimum d'échantillon est paramétrable."""
    if (
        not profile
        or profile["average"] >= TEAM_SOT_MAX_MEAN
        or profile.get("samples", 0) < min_samples
    ):
        return None
    best_probability, best_line = 0.0, None
    for line in (3.5, 4.5):
        probability, _ = line_probabilities(
            profile["average"], profile["variance"], line, profile["samples"]
        )
        if probability >= TEAM_SOT_MIN_PROBABILITY and probability > best_probability:
            best_probability, best_line = probability, line
    if best_line is None:
        return None
    return best_line, best_probability


def select_player_sot(profile, line_probabilities):
    """Reproduit les seuils production pour les sélections joueurs tirs cadrés."""
    if not profile:
        return None
    hit_rate = profile.get("hit_rate", 0)
    if (
        profile.get("samples", 0) < PLAYER_SOT_MIN_SAMPLES
        or profile.get("avg_minutes", 0) < PLAYER_SOT_MIN_AVG_MINUTES
        or hit_rate < PLAYER_SOT_MIN_HIT_RATE
        or not is_attacking_player(profile.get("position", ""))
    ):
        return None

    probability_05, _ = line_probabilities(
        profile["average"], profile["variance"], 0.5, profile["samples"]
    )
    probability_15, _ = line_probabilities(
        profile["average"], profile["variance"], 1.5, profile["samples"]
    )
    if probability_05 >= PLAYER_OVER_05_MIN_PROBABILITY and hit_rate >= PLAYER_OVER_05_MIN_HIT_RATE:
        return 0.5, probability_05
    if probability_15 >= PLAYER_OVER_15_MIN_PROBABILITY and hit_rate >= PLAYER_OVER_15_MIN_HIT_RATE:
        return 1.5, probability_15
    return None
