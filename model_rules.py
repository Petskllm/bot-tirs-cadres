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
CARDS_LINES = (3.5, 4.5)
CARDS_MIN_SAMPLES = 6
CARDS_MIN_PROBABILITY = 0.65


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


def resolve_player_lineup(availability, current_starters, previous_starters):
    """Préférer la compo officielle du match, sinon l'ancienne en l'étiquetant."""
    current = {str(player_id) for player_id in (current_starters or []) if player_id is not None}
    previous = {str(player_id) for player_id in (previous_starters or []) if player_id is not None}
    confirmed = is_confirmed_lineup(availability)
    if confirmed and current:
        return current, "composition confirmée"
    if previous:
        return previous, "dernière composition officielle connue — à vérifier"
    if confirmed:
        return set(), "composition confirmée indisponible"
    return set(), "composition du jour non confirmée"


def lineup_starter_ids(lineups, side=None):
    """Extrait les titulaires et tolère les compositions absentes ou nulles."""
    if not isinstance(lineups, dict):
        return set()
    payload = lineups.get("lineups", lineups)
    if not isinstance(payload, dict):
        return set()
    sides = (side,) if side in {"home", "away"} else ("home", "away")
    found = set()
    for team_side in sides:
        side_data = payload.get(team_side, {})
        if not isinstance(side_data, dict):
            continue
        players = side_data.get("players", [])
        if not isinstance(players, list):
            continue
        for player in players:
            if not isinstance(player, dict):
                continue
            player_id = player.get("id", player.get("player_id"))
            if player_id is not None:
                found.add(str(player_id))
    return found


def select_cards_markets(profile, line_probabilities):
    """Retourne chaque ligne over/under cartons atteignant les seuils prudents."""
    if not profile or profile.get("samples", 0) < CARDS_MIN_SAMPLES:
        return []
    try:
        mean = float(profile["average"])
        variance = float(profile["variance"])
        samples = int(profile["samples"])
    except (KeyError, TypeError, ValueError):
        return []

    candidates = []
    for line in CARDS_LINES:
        over_probability, under_probability = line_probabilities(
            mean, variance, line, samples
        )
        if over_probability >= CARDS_MIN_PROBABILITY:
            candidates.append((line, "over", over_probability))
        if under_probability >= CARDS_MIN_PROBABILITY:
            candidates.append((line, "under", under_probability))
    return sorted(candidates, key=lambda item: (-item[2], item[0], item[1]))


def cards_market_key(line, direction):
    """Identifiant distinct pour calibrer chaque ligne et chaque direction."""
    return f"cartons_{direction}_{str(float(line)).replace('.', '_')}"


def cards_selection_label(line, direction):
    """Libellé court commun à Telegram, au suivi et au backtest."""
    sign = "+" if direction == "over" else "-"
    return f"{sign}{float(line):.1f}".replace(".", ",")


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
