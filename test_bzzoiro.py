import os
import requests
import math
import time
from datetime import datetime, timedelta

# --- CONFIGURATION ---
TOKEN = os.getenv("BZZOIRO_TOKEN")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("CHAT_ID")
BASE = "https://sports.bzzoiro.com/api/v2"
headers = {"Authorization": f"Token {TOKEN}"}

if not all([TOKEN, TG_TOKEN, TG_CHAT]):
    print("❌ Secrets manquants")
    exit(1)

# --- FILTRE DES CHAMPIONNATS ---
LIGUES_AUTORISEES = [
    "premier league", "la liga", "laliga", "serie a", "bundesliga", "ligue 1",
    "champions league", "europa league", "conference league",
    "eredivisie", "primeira liga", "championship", "brasileirao", "ligue 2", "serie b", "la liga 2"
]

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
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"):
        return []

    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("event_date", ""), reverse=True)

    weighted_vals = []

    for idx, e in enumerate(finished[:10]):  # on regarde un peu plus large
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

def get_players_sot_history(team_id):
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"):
        return {}

    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("event_date", ""), reverse=True)

    player_history = {}
    for idx, e in enumerate(finished[:7]):
        stats = api_get(f"events/{e['id']}/player-stats/")
        if stats and "player_stats" in stats:
            weight = max(1.0, 1.5 - (idx * 0.1))
            for p in stats["player_stats"]:
                if p.get("team_id") == team_id and p.get("minutes_played", 0) >= 45:
                    name = p.get("player_name", "")
                    if not name or name == "Inconnu":
                        continue
                    sots = p.get("shots_on_target", 0)
                    if name not in player_history:
                        player_history[name] = []
                    player_history[name].append((sots, weight))

    weighted_players = {}
    for name, history in player_history.items():
        if len(history) >= 3:
            total_w = sum(w for v, w in history)
            avg = sum(v * w for v, w in history) / total_w
            weighted_players[name] = avg

    return weighted_players

def main():
    print("⏳ Récupération de la liste des ligues...")
    league_map = {}
    all_leagues = api_get("leagues/")
    if all_leagues and "results" in all_leagues:
        for l in all_leagues["results"]:
            league_map[l["id"]] = str(l.get("name", "")).lower()

    print("⏳ Analyse globale (version Domicile/Extérieur + xG)...")
    today = datetime.now().strftime("%Y-%m-%d")

    events = api_get(f"events/?date_from={today}&date_to={today}")
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

        if not any(l in league_name for l in LIGUES_AUTORISEES):
            matchs_ignores += 1
            continue

        matchs_analyses += 1
        fid = m.get("id")
        home_name = m.get("home_team")
        away_name = m.get("away_team")
        match_title = f"{home_name} — {away_name}"
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")

        # ============================================
        # 1. TIRS TOTAUX (avec séparation D/E + xG)
        # ============================================
        h_shots = get_team_stats(home_id, "total_shots", preferred_side="home")
        a_shots = get_team_stats(away_id, "total_shots", preferred_side="away")
        h_xg = get_team_stats(home_id, "xg", preferred_side="home")
        a_xg = get_team_stats(away_id, "xg", preferred_side="away")

        if h_shots and a_shots:
            # On mélange un peu les tirs réels et les xG pour plus de robustesse
            h_val = h_shots[0]
            a_val = a_shots[0]

            if h_xg:
                h_val = (h_shots[0] * 0.65) + (h_xg[0] * 8.5 * 0.35)  # approx conversion xG → tirs
            if a_xg:
                a_val = (a_shots[0] * 0.65) + (a_xg[0] * 8.5 * 0.35)

            match_total = h_val + a_val

            if 18 < match_total < 48:
                best_prob, best_line, best_cote = 0, None, None
                for line in [26.5, 27.5, 28.5, 29.5]:
                    prob = poisson_plus_de(match_total, line)
                    cote_estimee = round(1 / prob * 1.04, 2)
                    if prob >= 0.56 and cote_estimee >= 1.28 and prob > best_prob:
                        best_prob, best_line, best_cote = prob, line, cote_estimee

                if best_line:
                    picks_total.append(
                        f"⚽ {match_title}\n"
                        f"  Total attendu : {match_total:.1f}\n"
                        f"  • +{best_line} tirs : {best_prob:.0%} (Cote min : {best_cote})"
                    )
                    combo_pool_totals.append({
                        "match_id": fid,
                        "match_name": match_title,
                        "desc": f"+{best_line} tirs (Total)",
                        "prob": best_prob,
                        "cote": best_cote
                    })

        # ============================================
        # 2. TIRS CADRÉS ÉQUIPES (D/E)
        # ============================================
        h_sot = get_team_stats(home_id, "shots_on_target", preferred_side="home")
        a_sot = get_team_stats(away_id, "shots_on_target", preferred_side="away")

        if h_sot and a_sot:
            for team, avg, side in [(home_name, h_sot[0], "home"), (away_name, a_sot[0], "away")]:
                if avg < 12:
                    best_prob, best_line, best_cote = 0, None, None
                    for line in [3.5, 4.5]:
                        prob = poisson_plus_de(avg, line)
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
        h_corners = get_team_stats(home_id, "corner_kicks", preferred_side="home")
        a_corners = get_team_stats(away_id, "corner_kicks", preferred_side="away")

        if h_corners and a_corners:
            total_corners = h_corners[0] + a_corners[0]
            if 7 < total_corners < 22:
                best_prob, best_line, best_cote = 0, None, None
                for line in [9.5, 10.5, 11.5]:
                    prob = poisson_plus_de(total_corners, line)
                    cote_estimee = round(1 / prob * 1.04, 2)
                    if prob >= 0.61 and cote_estimee >= 1.28 and prob > best_prob:
                        best_prob, best_line, best_cote = prob, line, cote_estimee
                if best_line:
                    picks_corners.append(
                        f"🚩 {match_title}\n"
                        f"  Corners attendus : {total_corners:.1f}\n"
                        f"  • +{best_line} corners : {best_prob:.0%} (Cote min : {best_cote})"
                    )

        # ============================================
        # 4. CARTONS (D/E)
        # ============================================
        h_yellow = get_team_stats(home_id, "yellow_cards", preferred_side="home")
        a_yellow = get_team_stats(away_id, "yellow_cards", preferred_side="away")

        if h_yellow and a_yellow:
            total_cards = h_yellow[0] + a_yellow[0]
            if 2 < total_cards < 11:
                best_prob, best_line, best_cote = 0, None, None
                for line in [4.5, 5.5, 6.5]:
                    prob = poisson_plus_de(total_cards, line)
                    cote_estimee = round(1 / prob * 1.04, 2)
                    if prob >= 0.56 and cote_estimee >= 1.38 and prob > best_prob:
                        best_prob, best_line, best_cote = prob, line, cote_estimee
                if best_line:
                    picks_cartons.append(
                        f"🟨 {match_title}\n"
                        f"  Cartons attendus : {total_cards:.1f}\n"
                        f"  • +{best_line} cartons : {best_prob:.0%} (Cote min : {best_cote})"
                    )

        # ============================================
        # 5. JOUEURS (inchangé pour l’instant)
        # ============================================
        h_players = get_players_sot_history(home_id)
        a_players = get_players_sot_history(away_id)
        all_players = {**h_players, **a_players}

        for p_name, avg_sots in all_players.items():
            prob_05 = poisson_plus_de(avg_sots, 0.5)
            prob_15 = poisson_plus_de(avg_sots, 1.5)

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

    send_telegram(msg)
    print("✅ Message envoyé !")

if __name__ == "__main__":
    main()
