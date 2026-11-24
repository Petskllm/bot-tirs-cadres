import os
import requests
import math
import time
from functools import lru_cache
from datetime import datetime, timedelta, timezone

# --- CONFIGURATION ---
TOKEN = os.getenv("BZZOIRO_TOKEN")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "0").lower() in {"1", "true", "yes"}
BASE = "https://sports.bzzoiro.com/api/v2"
headers = {"Authorization": f"Token {TOKEN}"}


def utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

if not TOKEN or (not DRY_RUN and not all([TG_TOKEN, TG_CHAT])):
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

def poisson_plus_de(lam, k):
    if lam <= 0:
        return 0.0
    p = 0
    for i in range(int(k) + 1):
        p += math.exp(-lam) * (lam ** i) / math.factorial(i)
    return 1 - p

def send_telegram(msg):
    for i in range(0, len(msg), 4000):
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg[i:i+4000]},
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


def line_probabilities(mean, variance, line, samples):
    """Probabilités over/under avec Poisson de secours ou approximation normale.

    Pour au moins cinq observations, la dispersion historique est utilisée.
    La variance ne descend jamais sous la moyenne, qui est le plancher de la
    loi de Poisson pour un comptage.
    """
    if mean <= 0:
        return 0.0, 1.0
    if samples < 5:
        over = poisson_plus_de(mean, line)
        return over, 1 - over
    standard_deviation = math.sqrt(max(mean, variance, 1.0))
    z = (line - mean) / standard_deviation
    under = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return 1 - under, under


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


def lineup_starter_ids(lineups):
    """Extrait les titulaires Bzzoiro : players, hors substitutes."""
    found = set()
    payload = lineups.get("lineups", lineups) if isinstance(lineups, dict) else {}
    for side in ("home", "away"):
        players = payload.get(side, {}).get("players", [])
        for player in players if isinstance(players, list) else []:
            player_id = player.get("id", player.get("player_id"))
            if player_id:
                found.add(str(player_id))
    return found


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
    for idx, e in enumerate(finished[:7]):
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
                try:
                    sots = float(p.get("shots_on_target", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if name not in player_history:
                    player_history[name] = []
                    player_ids[name] = str(player_id)
                player_history[name].append((sots, weight, minutes))

    weighted_players = {}
    for name, history in player_history.items():
        if len(history) >= 3:
            total_w = sum(w for v, w, mins in history)
            avg = sum(v * w for v, w, mins in history) / total_w
            variance = sum(w * (v - avg) ** 2 for v, w, mins in history) / total_w
            avg_minutes = sum(mins * w for v, w, mins in history) / total_w
            if avg_minutes < 35:
                continue
            weighted_players[name] = {
                "average": avg,
                "variance": variance,
                "samples": len(history),
                "avg_minutes": avg_minutes,
                "player_id": player_ids.get(name),
            }

    return weighted_players

def main():
    print("⏳ Récupération de la liste des ligues...")
    league_map = {}
    all_leagues = api_get("leagues/")
    if all_leagues and "results" in all_leagues:
        for l in all_leagues["results"]:
            league_map[l["id"]] = str(l.get("name", "")).lower()

    print("⏳ Analyse globale (version Domicile/Extérieur + xG)...")
    today = utc_date()

    events = api_get(f"events/?date_from={today}&date_to={today}&limit=200")
    if not events or not events.get("results"):
        send_telegram(f"🤷‍♂️ Aucun match dans l'API pour le {today}.")
        return

    matches = events["results"]

    picks_total = []
    picks_cadres_eq = []
    picks_cadres_joueurs = []
    picks_corners = []
    picks_cartons = []
    combo_pool_totals = []

    matchs_analyses = 0
    matchs_ignores = 0
    start_time = time.time()

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
        lineup_status = availability.get("lineup_status") if isinstance(availability, dict) else None
        starters = set()
        if available.get("lineups") and lineup_status == "confirmed":
            starters = lineup_starter_ids(get_event_lineups(fid))
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

                if sample_count >= 6 and best_prob >= 0.58 and best_cote >= 1.28:
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

        # ============================================
        # 2. TIRS CADRÉS ÉQUIPES (D/E)
        # ============================================
        if h_profile.get("shots_on_target") and a_profile.get("shots_on_target"):
            for team, profile, side in [
                (home_name, h_profile["shots_on_target"], "home"),
                (away_name, a_profile["shots_on_target"], "away"),
            ]:
                avg = profile["average"]
                if avg < 12:
                    best_prob, best_line, best_cote = 0, None, None
                    for line in [3.5, 4.5]:
                        prob, _ = line_probabilities(
                            avg, profile["variance"], line, profile["samples"]
                        )
                        if prob >= 0.59 and prob > best_prob:
                            best_prob, best_line, best_cote = prob, line, round(1 / prob * 1.04, 2)
                    if best_line:
                        picks_cadres_eq.append(
                            f"⚽ {match_title}\n"
                            f"  • {team} +{best_line} : {best_prob:.0%} (Cote min : {best_cote})"
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
                if bzzoiro_corners_prob is not None:
                    best_prob = 0.70 * own_corners_prob + 0.30 * bzzoiro_corners_prob
                best_cote = round(1 / best_prob * 1.04, 2) if best_prob else None
                if best_prob >= 0.61 and best_cote >= 1.28:
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

        # ============================================
        # 4. CARTONS (D/E)
        # ============================================
        if h_profile.get("yellow_cards") and a_profile.get("yellow_cards"):
            h_yellow = h_profile["yellow_cards"]
            a_yellow = a_profile["yellow_cards"]
            referee_adjustment = referee_card_adjustment(m)
            total_cards = h_yellow["average"] + a_yellow["average"] + referee_adjustment
            cards_variance = h_yellow["variance"] + a_yellow["variance"]
            cards_samples = min(h_yellow["samples"], a_yellow["samples"])
            if 2 < total_cards < 11:
                line = 4.5
                best_prob, _ = line_probabilities(
                    total_cards, cards_variance, line, cards_samples
                )
                best_cote = round(1 / best_prob * 1.04, 2) if best_prob else None
                if best_prob >= 0.56 and best_cote >= 1.38:
                    picks_cartons.append(
                        f"🟨 {match_title}\n"
                        + f"  Cartons attendus : {total_cards:.1f}"
                        + (f" (arbitre : {referee_adjustment:+.1f})\n" if referee_adjustment else "\n")
                        + f"  • +4,5 cartons : {best_prob:.0%} (Cote estimée : {best_cote})"
                    )

        # ============================================
        # 5. JOUEURS (historique v2 par player_id et minutes jouées)
        # ============================================
        h_players = get_players_sot_history(home_id, l_id)
        a_players = get_players_sot_history(away_id, l_id)
        all_players = {**h_players, **a_players}

        for p_name, player_profile in all_players.items():
            if starters and player_profile.get("player_id") not in starters:
                continue
            avg_sots = player_profile["average"]
            prob_05, _ = line_probabilities(
                avg_sots, player_profile["variance"], 0.5, player_profile["samples"]
            )
            prob_15, _ = line_probabilities(
                avg_sots, player_profile["variance"], 1.5, player_profile["samples"]
            )

            if prob_05 >= 0.62:
                cote = round(1 / prob_05 * 1.04, 2)
                picks_cadres_joueurs.append(
                    f"⚽ {match_title}\n  • {p_name} +0,5 : {prob_05:.0%} (Cote min : {cote})"
                )
            elif prob_15 >= 0.52:
                cote = round(1 / prob_15 * 1.04, 2)
                picks_cadres_joueurs.append(
                    f"⚽ {match_title}\n  • {p_name} +1,5 : {prob_15:.0%} (Cote min : {cote})"
                )

        if matchs_analyses >= 14 or (time.time() - start_time) > 280:
            break

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
    msg += f"🔎 Matchs analysés : {matchs_analyses} (Top ligues)\n"
    msg += f"🚫 Autres matchs ignorés : {matchs_ignores}\n\n"

    if len(combo_picks) >= 3:
        msg += "🔥 **COMBINÉ TIRS TOTAUX (Max 5)** 🔥\n"
        for i, pick in enumerate(combo_picks, 1):
            msg += f"{i}. {pick['match_name']}\n   • {pick['desc']} : {pick['prob']:.0%} (Cote : {pick['cote']})\n"
        msg += f"💰 Cote totale estimée : {round(total_cote, 2)}\n"
        msg += "⚠️ _Uniquement total de tirs._\n\n"
    elif picks_total:
        msg += "⚠️ _Pas assez de matchs pour un combiné aujourd'hui._\n\n"

    if picks_total:
        msg += "📈 TIRS TOTAUX :\n" + "\n\n".join(picks_total[:5]) + "\n\n"

    if picks_corners:
        msg += "🚩 CORNERS :\n" + "\n\n".join(picks_corners[:3]) + "\n\n"

    if picks_cartons:
        msg += "🟨 CARTONS :\n" + "\n\n".join(picks_cartons[:3]) + "\n\n"

    if picks_cadres_eq:
        msg += "🎯 TIRS CADRÉS (Équipes) :\n" + "\n\n".join(picks_cadres_eq[:3]) + "\n\n"

    if picks_cadres_joueurs:
        msg += "👤 TIRS CADRÉS (Joueurs) :\n" + "\n\n".join(picks_cadres_joueurs[:4]) + "\n\n"

    if not any([picks_total, picks_cadres_eq, picks_cadres_joueurs, picks_corners, picks_cartons]):
        msg += "❌ Aucune sélection ne passe les filtres aujourd'hui."

    if DRY_RUN:
        print("🧪 DRY_RUN actif : aucun message Telegram envoyé")
        print(msg)
        return

    send_telegram(msg)
    print("✅ Message envoyé !")

if __name__ == "__main__":
    main()
