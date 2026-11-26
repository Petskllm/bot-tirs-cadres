import os
import requests
import time
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from tracking import build_reports, record_predictions, settle_pending
from api_football_validation import diagnose_api_football, format_report, validate_matches
from bot_modes import should_persist_results, should_send_telegram
from model_rules import (
    BZZOIRO_TEAM_SOT_MIN_SAMPLES,
    cards_market_key,
    cards_selection_label,
    is_attacking_player,
    is_confirmed_lineup,
    lineup_starter_ids,
    normalized_player_position,
    resolve_player_lineup,
    select_cards_markets,
    select_player_sot,
    select_team_sot,
)
from probability import line_probabilities
from telegram_message import build_telegram_messages

# --- CONFIGURATION ---
TOKEN = os.getenv("BZZOIRO_TOKEN")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "1").lower() in {"1", "true", "yes"}
TRACK_ONLY = os.getenv("TRACK_ONLY", "0").lower() in {"1", "true", "yes"}
API_FOOTBALL_DIAGNOSTIC = os.getenv("API_FOOTBALL_DIAGNOSTIC", "0").lower() in {"1", "true", "yes"}
BASE = "https://sports.bzzoiro.com/api/v2"
headers = {"Authorization": f"Token {TOKEN}"}
MODEL_VERSION = "v2.7-last-lineup-all-markets"

def utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

if (not TOKEN and not API_FOOTBALL_DIAGNOSTIC) or (
    not DRY_RUN and not TRACK_ONLY and not API_FOOTBALL_DIAGNOSTIC and not all([TG_TOKEN, TG_CHAT])
):
    print("❌ Secrets manquants")
    exit(1)

# --- FILTRE DES CHAMPIONNATS ---
LIGUES_AUTORISEES = [
    "premier league", "la liga", "laliga", "serie a", "bundesliga", "ligue 1",
    "champions league", "europa league", "conference league",
    "eredivisie", "primeira liga", "championship", "brasileirao", "ligue 2", "serie b", "la liga 2"
]

@lru_cache(maxsize=512)
def api_get(endpoint):
    try:
        r = requests.get(f"{BASE}/{endpoint}", headers=headers, timeout=15)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def get_daily_events(today, attempts=3, league_ids=None):
    """Récupère toute la journée par ligue suivie, avec relances et déduplication.

    Les réponses sont dédupliquées par identifiant, ce qui évite de manquer les
    ligues suivies lorsque le résultat global dépasse la limite de l'API.
    Retourne (payload, error), en conservant les résultats partiels éventuels.
    """
    league_ids = sorted({str(league_id) for league_id in (league_ids or [])})
    query_leagues = league_ids or [None]
    found = {}
    errors = []
    for league_id in query_leagues:
        endpoint = f"events/?date_from={today}&date_to={today}&limit=200"
        if league_id is not None:
            endpoint += f"&league_id={league_id}"
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(f"{BASE}/{endpoint}", headers=headers, timeout=15)
                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}"
                else:
                    payload = response.json()
                    results = payload.get("results") if isinstance(payload, dict) else None
                    if isinstance(results, list):
                        for event in results:
                            if isinstance(event, dict) and event.get("id") is not None:
                                found[str(event["id"])] = event
                        last_error = None
                        break
                    last_error = "liste de résultats invalide"
            except (requests.RequestException, ValueError) as exc:
                last_error = type(exc).__name__
            if attempt < attempts:
                time.sleep(2)
        if last_error:
            errors.append(f"ligue {league_id or 'globale'}: {last_error}")

    payload = {"results": list(found.values())}
    error = "; ".join(errors) if errors else None
    if not found and error:
        return None, error
    return payload, error

def send_telegram(messages):
    if isinstance(messages, str):
        messages = [messages]
    for msg in messages:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg},
            timeout=15,
        )
        response.raise_for_status()

def get_team_stats(team_id, stat_type, preferred_side=None):
    """
    preferred_side = "home" ou "away"
    Donne beaucoup plus de poids aux matchs joués du même côté.
    """
    today = utc_date()
    past = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}&limit=200")
    if not events or not events.get("results"):
        return []

    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("event_date", ""), reverse=True)

    weighted_vals = []

    for idx, e in enumerate(finished[:10]):  # on regarde un peu plus large
        availability = get_event_availability(e["id"])
        if availability.get("available", {}).get("stats") is False:
            continue
        st = api_get(f"events/{e['id']}/stats/")
        if not st or not st.get("stats"):
            continue

        is_home = (e["home_team_id"] == team_id)
        side = "home" if is_home else "away"

        # Poids de base (plus le match est récent, plus il pèse)
        base_weight = max(1.0, 1.6 - (idx * 0.12))

        # Bonus très important si même côté (domicile/extérieur)
        if preferred_side and side == preferred_side:
            weight = base_weight * 1.85
        else:
            weight = base_weight * 0.65   # les matchs de l'autre côté pèsent beaucoup moins

        if stat_type == "xg":
            xg_data = st["stats"].get(side, {}).get("xg")
            if isinstance(xg_data, dict):
                val = xg_data.get("actual")
            else:
                val = xg_data
        else:
            val = st["stats"].get(side, {}).get(stat_type)

        if val is not None:
            try:
                weighted_vals.append((float(val), weight))
            except:
                pass

    if not weighted_vals:
        return []

    total_weight = sum(w for v, w in weighted_vals)
    weighted_avg = sum(v * w for v, w in weighted_vals) / total_weight
    return [weighted_avg]


@lru_cache(maxsize=256)
def get_team_shot_profile(team_id, preferred_side, league_id=None):
    """Retourne moyenne et dispersion des tirs récents d'une équipe.

    Le profil utilise les statistiques de match disponibles dans l'API v2.
    La variance historique permet de ne pas traiter tous les matchs comme
    une loi de Poisson parfaitement régulière.
    """
    today = utc_date()
    past = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
    league_filter = f"&league_id={league_id}" if league_id else ""
    events = api_get(
        f"events/?team_id={team_id}&date_from={past}&date_to={today}{league_filter}&limit=200"
    )
    if not events or not events.get("results"):
        return {}

    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("event_date", ""), reverse=True)
    fields = (
        "total_shots", "shots_on_target", "shots_off_target", "blocked_shots",
        "hit_woodwork", "corner_kicks", "yellow_cards",
    )
    observations = {field: [] for field in fields}
    observations["xg"] = []

    for idx, event in enumerate(finished[:10]):
        availability = get_event_availability(event["id"])
        if availability.get("available", {}).get("stats") is False:
            continue
        stats = api_get(f"events/{event['id']}/stats/")
        if not stats or not stats.get("stats"):
            continue
        side = "home" if event.get("home_team_id") == team_id else "away"
        values = stats["stats"].get(side, {})
        base_weight = max(1.0, 1.6 - (idx * 0.12))
        weight = base_weight * (1.85 if side == preferred_side else 0.65)

        for field in fields:
            value = values.get(field)
            try:
                if value is not None:
                    observations[field].append((float(value), weight))
            except (TypeError, ValueError):
                continue

        xg = values.get("xg")
        if isinstance(xg, dict):
            xg = xg.get("actual")
        try:
            if xg is not None:
                observations["xg"].append((float(xg), weight))
        except (TypeError, ValueError):
            pass

    profile = {}
    for field, values in observations.items():
        if not values:
            continue
        total_weight = sum(weight for value, weight in values)
        average = sum(value * weight for value, weight in values) / total_weight
        variance = sum(weight * (value - average) ** 2 for value, weight in values) / total_weight
        profile[field] = {
            "average": average,
            "variance": variance,
            "samples": len(values),
        }

    # Certains matchs ne renseignent pas total_shots mais fournissent les
    # composantes. On reconstruit alors une estimation prudente.
    if "total_shots" not in profile:
        components = [profile.get(field) for field in ("shots_on_target", "shots_off_target", "blocked_shots")]
        if all(components):
            profile["total_shots"] = {
                "average": sum(item["average"] for item in components),
                "variance": sum(item["variance"] for item in components),
                "samples": min(item["samples"] for item in components),
            }
    return profile


def _attacking_absences(payload, side):
    """Compte seulement les absences offensives explicitement identifiées."""
    if not isinstance(payload, dict):
        return 0
    candidates = payload.get(side)
    if candidates is None and isinstance(payload.get("unavailable_players"), dict):
        candidates = payload["unavailable_players"].get(side)
    if not isinstance(candidates, list):
        return 0

    total = 0
    attacking_positions = {"f", "fw", "m", "mf", "attacker", "forward", "midfielder"}
    for player in candidates:
        if not isinstance(player, dict):
            continue
        status = str(player.get("availability", player.get("status", ""))).lower()
        position = str(player.get("position", player.get("role", ""))).lower()
        if status in {"injured", "suspended", "doubtful", "unavailable"} and position in attacking_positions:
            total += 1
    return total


@lru_cache(maxsize=256)
def get_event_availability(event_id):
    """Récupère les absences sans faire échouer une analyse si l'endpoint est vide."""
    return api_get(f"events/{event_id}/availability/") or {}


@lru_cache(maxsize=256)
def get_event_lineups(event_id):
    return api_get(f"events/{event_id}/lineups/") or {}


@lru_cache(maxsize=256)
def get_event_prediction(event_id):
    return api_get(f"events/{event_id}/prediction/") or {}


@lru_cache(maxsize=256)
def get_event_odds(event_id):
    return api_get(f"events/{event_id}/odds/") or {}


def bzzoiro_corner_probability(prediction):
    """Lit la probabilité Bzzoiro (0-100) sans supposer qu'elle existe."""
    markets = prediction.get("markets", {}) if isinstance(prediction, dict) else {}
    corners = markets.get("corners", {}) if isinstance(markets, dict) else {}
    try:
        probability = float(corners.get("prob_over_95")) / 100
        confidence = float(prediction.get("model", {}).get("confidence", 0))
    except (TypeError, ValueError, AttributeError):
        return None
    if 0 < probability < 1 and confidence >= 0.55:
        return probability
    return None


def bzzoiro_consensus_corner_odds(odds):
    """Retourne la cote consensus +9,5 si elle existe dans la réponse."""
    rows = odds.get("results", odds) if isinstance(odds, dict) else odds
    if isinstance(rows, dict):
        rows = rows.get("odds", rows.get("markets", []))
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market", row.get("key", ""))).lower()
        outcome = str(row.get("outcome", row.get("name", ""))).lower()
        try:
            line = float(row.get("line"))
            price = float(row.get("decimal_odds", row.get("odds", row.get("price"))))
        except (TypeError, ValueError):
            continue
        if "corner" in market and abs(line - 9.5) < 0.01 and "over" in outcome and price > 1:
            return price
    return None


@lru_cache(maxsize=256)
def get_last_official_starters(team_id):
    """Retourne les titulaires du dernier match terminé avec une composition."""
    today = utc_date()
    past = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
    events = api_get(
        f"events/?team_id={team_id}&date_from={past}&date_to={today}&limit=50"
    )
    if not events or not isinstance(events.get("results"), list):
        return set()
    finished = [event for event in events["results"] if event.get("status") == "finished"]
    finished.sort(key=lambda event: event.get("event_date", ""), reverse=True)
    for event in finished[:10]:
        lineups = get_event_lineups(event.get("id"))
        for side in ("home", "away"):
            if str(event.get(f"{side}_team_id")) != str(team_id):
                continue
            starters = lineup_starter_ids(lineups, side)
            if starters:
                return starters
    return set()


def attacking_shot_adjustment(event, side):
    """Réduit prudemment le volume attendu si une absence offensive est certaine."""
    payload = event.get("unavailable_players")
    if not payload:
        payload = get_event_availability(event.get("id"))
    missing = _attacking_absences(payload, side)
    return -min(2.0, missing * 0.35)


@lru_cache(maxsize=256)
def get_referee(referee_id):
    return api_get(f"referees/{referee_id}/") or {}


def referee_card_adjustment(event):
    """Ajuste limité des cartons si la moyenne de l'arbitre est fiable."""
    referee = event.get("referee")
    if not referee:
        return 0.0
    if isinstance(referee, dict):
        referee_id = referee.get("id")
        data = referee
    else:
        referee_id = referee
        data = {}
    if referee_id:
        remote = get_referee(referee_id)
        if isinstance(remote, dict):
            data = {**data, **remote}
            if isinstance(remote.get("referee"), dict):
                data = {**data, **remote["referee"]}
    try:
        average = float(data.get("avg_yellow_per_match"))
        samples = float(data.get("matches", data.get("career_games", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0
    if samples < 8 or not 0 < average < 12:
        return 0.0
    return max(-0.6, min(0.6, (average - 3.8) * 0.30))


@lru_cache(maxsize=1024)
def get_player_name(player_id):
    """Résout le nom v2 d'un joueur, dont player-stats ne renvoie que l'ID."""
    data = api_get(f"players/{player_id}/") or {}
    candidates = [data]
    if isinstance(data.get("player"), dict):
        candidates.append(data["player"])
    for item in candidates:
        for key in ("name", "display_name", "player_name"):
            value = item.get(key)
            if value:
                return str(value)
        first = item.get("first_name") or item.get("firstname")
        last = item.get("last_name") or item.get("lastname")
        if first or last:
            return " ".join(str(x) for x in (first, last) if x)
    return f"Joueur {player_id}"


@lru_cache(maxsize=1024)
def get_player_profile(player_id):
    """Retourne le profil Bzzoiro d'un joueur quand il est disponible."""
    data = api_get(f"players/{player_id}/") or {}
    if isinstance(data.get("player"), dict):
        return {**data, **data["player"]}
    return data if isinstance(data, dict) else {}


def get_players_sot_history(team_id, league_id=None):
    today = utc_date()
    past = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    league_filter = f"&league_id={league_id}" if league_id else ""
    events = api_get(
        f"events/?team_id={team_id}&date_from={past}&date_to={today}{league_filter}&limit=200"
    )
    if not events or not events.get("results"):
        return {}

    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("event_date", ""), reverse=True)

    player_history = {}
    player_ids = {}
    for idx, e in enumerate(finished[:10]):
        availability = get_event_availability(e["id"])
        if availability.get("available", {}).get("player_stats") is False:
            continue
        stats = api_get(f"events/{e['id']}/player-stats/")
        if stats and "player_stats" in stats:
            weight = max(1.0, 1.5 - (idx * 0.1))
            for p in stats["player_stats"]:
                if p.get("team_id") != team_id:
                    continue
                try:
                    minutes = float(p.get("minutes_played", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if minutes < 30:
                    continue
                player_id = p.get("player_id")
                if not player_id:
                    continue
                name = get_player_name(player_id)
                if not name:
                    continue
                profile = get_player_profile(player_id)
                position = normalized_player_position(
                    {"position": p.get("position", p.get("role", ""))}
                ) or normalized_player_position(profile)
                try:
                    sots = float(p.get("shots_on_target", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if name not in player_history:
                    player_history[name] = []
                    player_ids[name] = str(player_id)
                player_history[name].append((sots, weight, minutes, position))

    weighted_players = {}
    for name, history in player_history.items():
        if len(history) < 4:
            continue
        positions = [position for _, _, _, position in history if position]
        # L'historique est parcouru du plus récent au plus ancien.
        position = positions[0] if positions else ""
        if not is_attacking_player(position):
            continue
        total_w = sum(w for v, w, mins, pos in history)
        avg = sum(v * w for v, w, mins, pos in history) / total_w
        variance = sum(w * (v - avg) ** 2 for v, w, mins, pos in history) / total_w
        avg_minutes = sum(mins * w for v, w, mins, pos in history) / total_w
        hit_rate = sum(w for v, w, mins, pos in history if v >= 1.0) / total_w
        if avg_minutes < 55:
            continue
        weighted_players[name] = {
            "average": avg,
            "variance": variance,
            "samples": len(history),
            "avg_minutes": avg_minutes,
            "hit_rate": hit_rate,
            "position": position,
            "player_id": player_ids.get(name),
        }

    return weighted_players

def main():
    if API_FOOTBALL_DIAGNOSTIC:
        import json
        print("🔬 Diagnostic API-Football uniquement : aucun Telegram, aucun pronostic")
        print(json.dumps(diagnose_api_football(utc_date()), ensure_ascii=False, indent=2))
        return
    if should_persist_results(DRY_RUN, TRACK_ONLY):
        settled = settle_pending(api_get)
        if settled:
            print(f"✅ Pronostics réglés : {settled}")
        build_reports()
    print("⏳ Récupération de la liste des ligues...")
    league_map = {}
    all_leagues = api_get("leagues/")
    if all_leagues and "results" in all_leagues:
        for l in all_leagues["results"]:
            league_map[l["id"]] = str(l.get("name", "")).lower()

    print("⏳ Analyse globale (version Domicile/Extérieur + xG)...")
    today = utc_date()
    approved_league_ids = [
        league_id for league_id, name in league_map.items()
        if " ".join(name.split()) in LIGUES_AUTORISEES
    ]
    events, events_error = get_daily_events(today, league_ids=approved_league_ids)
    if events_error and events and events.get("results"):
        print(f"⚠️ Réponse partielle de l'API : {events_error}")
    if not events or not events.get("results"):
        fallback = validate_matches([], today)
        api_count = fallback.get("api_fixtures", 0) if fallback.get("enabled") else 0
        if events_error:
            message = (
                f"⚠️ Bzzoiro indisponible pour le {today} après 3 tentatives "
                f"({events_error}). API-Football confirme {api_count} fixture(s). "
                "Aucun pronostic généré."
            )
        elif api_count:
            message = (
                f"⚠️ Bzzoiro n'a renvoyé aucun match pour le {today}, "
                f"mais API-Football confirme {api_count} fixture(s). "
                "Aucun pronostic généré faute de données Bzzoiro complètes."
            )
        else:
            message = f"🤷‍♂️ Aucune fixture confirmée par les APIs pour le {today}."
        if not should_send_telegram(DRY_RUN, TRACK_ONLY):
            print("🧪 Mode sans publication Telegram actif")
            print(message)
        else:
            send_telegram(message)
        return

    matches = sorted(
        events["results"], key=lambda match: match.get("event_date") or ""
    )
    validation_matches = []
    for match in matches:
        if match.get("status") != "notstarted":
            continue
        match_league = " ".join(league_map.get(match.get("league_id"), "").split())
        if match_league in LIGUES_AUTORISEES:
            validation_matches.append(match)
    api_football_report = validate_matches(validation_matches, today)

    picks_total = []
    picks_cadres_eq = []
    picks_cadres_joueurs = []
    picks_corners = []
    picks_cartons = []
    combo_pool_totals = []
    prediction_records = []

    def track_prediction(**values):
        values.setdefault("source", "bzzoiro")
        prediction_records.append(values)

    matchs_analyses = 0
    matchs_ignores = 0

    for m in matches:
        if m.get("status") != "notstarted":
            continue

        l_id = m.get("league_id")
        league_name = league_map.get(l_id, "")

        league_name = " ".join(league_name.split())
        if league_name not in LIGUES_AUTORISEES:
            matchs_ignores += 1
            continue

        matchs_analyses += 1
        fid = m.get("id")
        home_name = m.get("home_team")
        away_name = m.get("away_team")
        match_title = f"{home_name} — {away_name}"
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        availability = get_event_availability(fid)
        available = availability.get("available", {}) if isinstance(availability, dict) else {}
        confirmed_today = is_confirmed_lineup(availability)
        current_starters = lineup_starter_ids(get_event_lineups(fid)) if confirmed_today else set()
        previous_starters = set()
        if not current_starters:
            if home_id:
                previous_starters |= get_last_official_starters(home_id)
            if away_id:
                previous_starters |= get_last_official_starters(away_id)
        starters, lineup_source = resolve_player_lineup(
            availability, current_starters, previous_starters
        )
        prediction = get_event_prediction(fid) if available.get("prediction") else {}
        odds = get_event_odds(fid) if available.get("odds") else {}
        bzzoiro_corners_prob = bzzoiro_corner_probability(prediction)
        bzzoiro_corners_odds = bzzoiro_consensus_corner_odds(odds)

        # ============================================
        # 1. TIRS TOTAUX (avec séparation D/E + xG)
        # ============================================
        h_profile = get_team_shot_profile(home_id, "home", l_id)
        a_profile = get_team_shot_profile(away_id, "away", l_id)

        if h_profile.get("total_shots") and a_profile.get("total_shots"):
            # Les tirs restent la variable cible. L'xG est conservé comme
            # information complémentaire, mais n'est plus converti avec le
            # facteur arbitraire 8,5 qui pouvait déformer le total attendu.
            h_shot_stat = h_profile["total_shots"]
            a_shot_stat = a_profile["total_shots"]
            h_val = max(0.0, h_shot_stat["average"] + attacking_shot_adjustment(m, "home"))
            a_val = max(0.0, a_shot_stat["average"] + attacking_shot_adjustment(m, "away"))
            match_total = h_val + a_val
            match_variance = h_shot_stat["variance"] + a_shot_stat["variance"]
            sample_count = min(h_shot_stat["samples"], a_shot_stat["samples"])

            if 18 < match_total < 48:
                # Ligne principale : plus de 27 = au moins 27 tirs,
                # moins de 27 = au plus 26 tirs. Les deux probabilités
                # sont complémentaires sur la ligne 26,5.
                line = 26.5
                raw_over, raw_under = line_probabilities(
                    match_total, match_variance, line, sample_count
                )
                confidence = min(1.0, sample_count / 8.0)
                over_prob = 0.5 + (raw_over - 0.5) * confidence
                under_prob = 0.5 + (raw_under - 0.5) * confidence
                candidates = [
                    ("+27", over_prob),
                    ("-27", under_prob),
                ]
                best_label, best_prob = max(candidates, key=lambda item: item[1])
                best_cote = round(1 / best_prob * 1.04, 2) if best_prob else None

                if sample_count >= 6 and best_prob >= 0.65 and best_cote >= 1.28:
                    picks_total.append(
                        f"⚽ {match_title}\n"
                        f"  Total attendu : {match_total:.1f}\n"
                        f"  • +27 tirs : {over_prob:.0%} | -27 tirs : {under_prob:.0%}\n"
                        f"  • Sélection : {best_label} tirs — {best_prob:.0%} (Cote estimée : {best_cote})"
                    )
                    combo_pool_totals.append({
                        "match_id": fid,
                        "match_name": match_title,
                        "desc": f"{best_label} tirs (Total)",
                        "prob": best_prob,
                        "cote": best_cote
                    })
                    track_prediction(
                        event_id=fid, kickoff=m.get("event_date"), match=match_title,
                        market="total_tirs", selection=best_label, line=26.5,
                        direction="over" if best_label == "+27" else "under",
                        probability=best_prob, estimated_odds=best_cote,
                        samples=sample_count,
                    )

        # ============================================
        # 2. TIRS CADRÉS ÉQUIPES (D/E)
        # ============================================
        if h_profile.get("shots_on_target") and a_profile.get("shots_on_target"):
            for team, profile, side in [
                (home_name, h_profile["shots_on_target"], "home"),
                (away_name, a_profile["shots_on_target"], "away"),
            ]:
                avg = profile["average"]
                selection = select_team_sot(
                    profile, line_probabilities,
                    min_samples=BZZOIRO_TEAM_SOT_MIN_SAMPLES,
                )
                if selection:
                    best_line, best_prob = selection
                    best_cote = round(1 / best_prob * 1.04, 2)
                    picks_cadres_eq.append(
                        f"⚽ {match_title}\n"
                        f"  • {team} +{best_line} : {best_prob:.0%} (Cote min : {best_cote})"
                    )
                    track_prediction(
                        event_id=fid, kickoff=m.get("event_date"), match=match_title,
                        market="tirs_cadres_equipe", selection=f"{team} +{best_line}",
                        line=best_line, direction="over", probability=best_prob,
                        estimated_odds=best_cote, team_id=home_id if side == "home" else away_id,
                        side=side, samples=profile["samples"],
                        expected_mean=profile["average"], expected_variance=profile["variance"],
                    )

        # ============================================
        # 3. CORNERS (D/E)
        # ============================================
        if h_profile.get("corner_kicks") and a_profile.get("corner_kicks"):
            h_corners = h_profile["corner_kicks"]
            a_corners = a_profile["corner_kicks"]
            total_corners = h_corners["average"] + a_corners["average"]
            corners_variance = h_corners["variance"] + a_corners["variance"]
            corners_samples = min(h_corners["samples"], a_corners["samples"])
            if 7 < total_corners < 22:
                line = 9.5
                own_corners_prob, _ = line_probabilities(
                    total_corners, corners_variance, line, corners_samples
                )
                best_prob = own_corners_prob
                best_cote = round(1 / best_prob * 1.04, 2) if best_prob else None
                if best_prob >= 0.65 and best_cote >= 1.28:
                    confirmation = ""
                    if bzzoiro_corners_prob is not None:
                        confirmation = f" | Bzzoiro : {bzzoiro_corners_prob:.0%}"
                    if bzzoiro_corners_odds:
                        confirmation += f" | Cote Bzzoiro : {bzzoiro_corners_odds:.2f}"
                    picks_corners.append(
                        f"🚩 {match_title}\n"
                        f"  Corners attendus : {total_corners:.1f}\n"
                        f"  • +9,5 corners : {best_prob:.0%} (Cote estimée : {best_cote}){confirmation}"
                    )
                    track_prediction(
                        event_id=fid, kickoff=m.get("event_date"), match=match_title,
                        market="corners", selection="+9,5", line=9.5, direction="over",
                        probability=best_prob, estimated_odds=best_cote,
                        samples=corners_samples, bzzoiro_probability=bzzoiro_corners_prob,
                        consensus_odds=bzzoiro_corners_odds,
                    )

        # ============================================
        # 4. CARTONS (D/E)
        # ============================================
        if h_profile.get("yellow_cards") and a_profile.get("yellow_cards"):
            h_yellow = h_profile["yellow_cards"]
            a_yellow = a_profile["yellow_cards"]
            referee_adjustment = referee_card_adjustment(m)
            total_cards = max(0.0, h_yellow["average"] + a_yellow["average"] + referee_adjustment)
            cards_variance = h_yellow["variance"] + a_yellow["variance"]
            cards_samples = min(h_yellow["samples"], a_yellow["samples"])
            if total_cards < 11:
                card_profile = {
                    "average": total_cards,
                    "variance": cards_variance,
                    "samples": cards_samples,
                }
                for line, direction, probability in select_cards_markets(
                    card_profile, line_probabilities
                ):
                    selection = cards_selection_label(line, direction)
                    market = cards_market_key(line, direction)
                    cote = round(1 / probability * 1.04, 2)
                    verb = "Plus de" if direction == "over" else "Moins de"
                    picks_cartons.append(
                        f"🟨 {match_title}\n"
                        + f"  Cartons attendus : {total_cards:.1f}"
                        + (f" (arbitre : {referee_adjustment:+.1f})\n" if referee_adjustment else "\n")
                        + f"  • {verb} {str(line).replace('.', ',')} cartons : {probability:.0%} (Cote estimée : {cote})"
                    )
                    track_prediction(
                        event_id=fid, kickoff=m.get("event_date"), match=match_title,
                        market=market, selection=selection, line=line, direction=direction,
                        probability=probability, estimated_odds=cote,
                        samples=cards_samples, referee_adjustment=referee_adjustment,
                    )

        # ============================================
        # 5. JOUEURS (historique v2 par player_id et minutes jouées)
        # ============================================
        h_players = get_players_sot_history(home_id, l_id)
        a_players = get_players_sot_history(away_id, l_id)
        all_players = {**h_players, **a_players}

        # Sans composition du jour ni dernière composition officielle, on ne
        # peut pas valider le temps de jeu : aucun marché joueur n'est publié.
        if not starters:
            all_players = {}

        for p_name, player_profile in all_players.items():
            if starters and player_profile.get("player_id") not in starters:
                continue
            selection = select_player_sot(player_profile, line_probabilities)
            if not selection:
                continue
            avg_sots = player_profile["average"]
            line, probability = selection
            if line == 0.5:
                prob_05 = probability
                cote = round(1 / prob_05 * 1.04, 2)
                picks_cadres_joueurs.append(
                    f"⚽ {match_title}\n  • {p_name} +0,5 : {prob_05:.0%} "
                    f"(Cote min : {cote}, {lineup_source}, historique : {player_profile['hit_rate']:.0%})"
                )
                track_prediction(
                    event_id=fid, kickoff=m.get("event_date"), match=match_title,
                    market="tirs_cadres_joueur", selection=f"{p_name} +0,5", line=0.5,
                    direction="over", probability=prob_05, estimated_odds=cote,
                    player_id=player_profile.get("player_id"), team_id=None,
                    samples=player_profile["samples"], avg_minutes=player_profile.get("avg_minutes"),
                    lineup_source=lineup_source, historical_hit_rate=player_profile.get("hit_rate"),
                    expected_mean=avg_sots, expected_variance=player_profile["variance"],
                )
            elif line == 1.5:
                prob_15 = probability
                cote = round(1 / prob_15 * 1.04, 2)
                picks_cadres_joueurs.append(
                    f"⚽ {match_title}\n  • {p_name} +1,5 : {prob_15:.0%} "
                    f"(Cote min : {cote}, {lineup_source}, historique : {player_profile['hit_rate']:.0%})"
                )
                track_prediction(
                    event_id=fid, kickoff=m.get("event_date"), match=match_title,
                    market="tirs_cadres_joueur", selection=f"{p_name} +1,5", line=1.5,
                    direction="over", probability=prob_15, estimated_odds=cote,
                    player_id=player_profile.get("player_id"), team_id=None,
                    samples=player_profile["samples"], avg_minutes=player_profile.get("avg_minutes"),
                    lineup_source=lineup_source, historical_hit_rate=player_profile.get("hit_rate"),
                    expected_mean=avg_sots, expected_variance=player_profile["variance"],
                )

    # --- COMBINÉ TIRS TOTAUX ---
    combo_pool_totals.sort(key=lambda x: -x["prob"])
    combo_picks = []
    used_matches = set()
    total_cote = 1.0

    for pick in combo_pool_totals:
        if pick["match_id"] not in used_matches:
            combo_picks.append(pick)
            used_matches.add(pick["match_id"])
            total_cote *= pick["cote"]
            if len(combo_picks) == 5:
                break

    # --- MESSAGE TELEGRAM ---
    msg = f"🎯 PRONOS TIRS & xG (Version D/E + xG)\n📅 {datetime.now().strftime('%d/%m/%Y')}\n"
    msg += "🕒 Périmètre : matchs non commencés de toute la journée (ligues suivies)\n"
    msg += f"🔎 Matchs analysés : {matchs_analyses} (Top ligues)\n"
    msg += f"🚫 Autres matchs ignorés : {matchs_ignores}\n\n"
    msg += format_report(api_football_report) + "\n\n"

    if len(combo_picks) >= 3:
        msg += "🔥 **COMBINÉ TIRS TOTAUX (Max 5)** 🔥\n"
        for i, pick in enumerate(combo_picks, 1):
            msg += f"{i}. {pick['match_name']}\n   • {pick['desc']} : {pick['prob']:.0%} (Cote : {pick['cote']})\n"
        msg += f"💰 Cote totale estimée : {round(total_cote, 2)}\n"
        msg += "⚠️ _Uniquement total de tirs._\n\n"
    elif picks_total:
        msg += "⚠️ _Pas assez de matchs pour un combiné aujourd'hui._\n\n"

    if not any([picks_total, picks_cadres_eq, picks_cadres_joueurs, picks_corners, picks_cartons]):
        msg += "❌ Aucune sélection ne passe les filtres aujourd'hui."

    sections = [
        ("📈 TIRS TOTAUX :", picks_total),
        ("🚩 CORNERS :", picks_corners),
        ("🟨 CARTONS :", picks_cartons),
        ("🎯 TIRS CADRÉS (Équipes) :", picks_cadres_eq),
        ("👤 TIRS CADRÉS (Joueurs) :", picks_cadres_joueurs),
    ]
    messages = build_telegram_messages(msg, sections)

    if not should_persist_results(DRY_RUN, TRACK_ONLY):
        print("🧪 DRY_RUN actif : aucun message Telegram envoyé")
        print("\n\n--- message suivant ---\n\n".join(messages))
        return

    added = record_predictions(prediction_records, MODEL_VERSION)
    report = build_reports()
    print(f"📚 Nouvelles sélections enregistrées : {added}")
    print(f"📊 Pronostics réglés suivis : {report.get('settled_predictions', 0)}")
    if not should_send_telegram(DRY_RUN, TRACK_ONLY):
        print("🧪 Mode suivi seul : aucun message Telegram envoyé")
        return
    send_telegram(messages)
    print("✅ Message envoyé !")

if __name__ == "__main__":
    main()
