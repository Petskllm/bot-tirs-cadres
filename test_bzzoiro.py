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

def api_get(endpoint):
    try:
        r = requests.get(f"{BASE}/{endpoint}", headers=headers, timeout=15)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def poisson_plus_de(lam, k):
    if lam <= 0: return 0.0
    p = 0
    for i in range(int(k) + 1):
        p += math.exp(-lam) * (lam ** i) / math.factorial(i)
    return 1 - p

def send_telegram(msg):
    for i in range(0, len(msg), 4000):
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg[i:i+4000]})

def get_team_stats(team_id, stat_type):
    """Récupère les stats avec pondération temporelle."""
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"): return []
    
    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("event_date", ""), reverse=True)
    
    weighted_vals = []
    for idx, e in enumerate(finished[:6]):
        st = api_get(f"events/{e['id']}/stats/")
        if st and st.get("stats"):
            is_home = (e["home_team_id"] == team_id)
            side = "home" if is_home else "away"
            
            # --- CORRECTION ICI ---
            # Si on cherche "xg", on doit aller chercher la valeur "actual" dans le dictionnaire
            if stat_type == "xg":
                xg_data = st["stats"].get(side, {}).get("xg")
                if isinstance(xg_data, dict):
                    val = xg_data.get("actual") # On prend la vraie valeur
                else:
                    val = xg_data
            else:
                val = st["stats"].get(side, {}).get(stat_type)
            # ----------------------

            if val is not None:
                weight = max(1.0, 1.5 - (idx * 0.1))
                weighted_vals.append((val, weight))
    
    if not weighted_vals: return []
    
    total_weight = sum(w for v, w in weighted_vals)
    weighted_avg = sum(v * w for v, w in weighted_vals) / total_weight
    return [weighted_avg]

def get_players_sot_history(team_id):
    """Historique des joueurs avec pondération temporelle."""
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"): return {}
    
    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("event_date", ""), reverse=True)
    
    player_history = {}
    for idx, e in enumerate(finished[:6]):
        stats = api_get(f"events/{e['id']}/player-stats/")
        if stats and "player_stats" in stats:
            weight = max(1.0, 1.5 - (idx * 0.1))
            for p in stats["player_stats"]:
                if p.get("team_id") == team_id and p.get("minutes_played", 0) >= 45:
                    name = p.get("player_name", "Inconnu")
                    sots = p.get("shots_on_target", 0)
                    if name not in player_history: player_history[name] = []
                    player_history[name].append((sots, weight))
                    
    weighted_players = {}
    for name, history in player_history.items():
        if len(history) >= 3:
            total_w = sum(w for v, w in history)
            avg = sum(v * w for v, w in history) / total_w
            weighted_players[name] = avg
            
    return weighted_players

def main():
    print("⏳ Analyse globale (xG, Pondération, Sécurité)...")
    today = datetime.now().strftime("%Y-%m-%d")
    
    events = api_get(f"events/?date_from={today}&date_to={today}")
    if not events or not events.get("results"):
        send_telegram(f"🤷‍♂️ Aucun match dans l'API pour le {today}.")
        return
    
    matches = events["results"]
    
    picks_total = []
    picks_cadres_eq = []
    picks_xg = []
    picks_cadres_joueurs = []
    matchs_analyses = 0
    start_time = time.time()
    
    for m in matches:
        if m.get("status") != "notstarted": continue
            
        matchs_analyses += 1
            
        home_name = m.get("home_team")
        away_name = m.get("away_team")
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        
        # 1. TIRS TOTAUX
        h_total = get_team_stats(home_id, "total_shots")
        a_total = get_team_stats(away_id, "total_shots")
        if h_total and a_total:
            match_total = h_total[0] + a_total[0]
            if 10 < match_total < 50:
                lines_tot = []
                for line in [27.5, 28.5, 29.5]:
                    prob = poisson_plus_de(match_total, line)
                    if prob >= 0.60:
                        lines_tot.append(f"  • +{line} tirs : {prob:.0%} (Cote min : {round(1/prob*1.05, 2)})")
                if lines_tot:
                    picks_total.append(f"⚽ {home_name} — {away_name}\n  Total attendu : {match_total:.1f} tirs\n" + "\n".join(lines_tot))

        # 2. TIRS CADRÉS ÉQUIPES
        h_sot = get_team_stats(home_id, "shots_on_target")
        a_sot = get_team_stats(away_id, "shots_on_target")
        if h_sot and a_sot:
            h_avg, a_avg = h_sot[0], a_sot[0]
            if h_avg < 15 and a_avg < 15:
                lines_eq = []
                for line in [3.5, 4.5]:
                    p_h, p_a = poisson_plus_de(h_avg, line), poisson_plus_de(a_avg, line)
                    if p_h >= 0.58: lines_eq.append(f"  • {home_name} +{line} : {p_h:.0%} (Cote min : {round(1/p_h*1.05, 2)})")
                    if p_a >= 0.58: lines_eq.append(f"  • {away_name} +{line} : {p_a:.0%} (Cote min : {round(1/p_a*1.05, 2)})")
                if lines_eq:
                    picks_cadres_eq.append(f"⚽ {home_name} — {away_name}\n" + "\n".join(lines_eq))

        # 3. xG
        h_xg = get_team_stats(home_id, "xg")
        a_xg = get_team_stats(away_id, "xg")
        if h_xg and a_xg:
            h_xg_avg, a_xg_avg = h_xg[0], a_xg[0]
            if h_xg_avg < 4.0 and a_xg_avg < 4.0:
                total_shots_estimated = (h_xg_avg * 8 + 5) + (a_xg_avg * 8 + 5)
                prob_xg = poisson_plus_de(total_shots_estimated, 27.5)
                if prob_xg >= 0.65:
                    picks_xg.append(f"⚽ {home_name} — {away_name}\n  xG Attendus : {h_xg_avg + a_xg_avg:.2f}\n  • +27,5 tirs estimés : {prob_xg:.0%} (Cote min : {round(1/prob_xg*1.05, 2)})")

        # 4. JOUEURS
        h_players = get_players_sot_history(home_id)
        a_players = get_players_sot_history(away_id)
        lines_joueurs = []
        all_players = {**h_players, **a_players}
        
        for p_name, avg_sots in all_players.items():
            prob_05 = poisson_plus_de(avg_sots, 0.5)
            prob_15 = poisson_plus_de(avg_sots, 1.5)
            
            if prob_05 >= 0.60:
                lines_joueurs.append(f"  • {p_name} +0,5 : {prob_05:.0%} (Cote min : {round(1/prob_05*1.05, 2)})")
            if prob_15 >= 0.50:
                lines_joueurs.append(f"  • {p_name} +1,5 : {prob_15:.0%} (Cote min : {round(1/prob_15*1.05, 2)})")
        
        if lines_joueurs:
            picks_cadres_joueurs.append(f"⚽ {home_name} — {away_name}\n" + "\n".join(lines_joueurs[:5]))

        if matchs_analyses >= 15 or (time.time() - start_time) > 300:
            break

    # --- MESSAGE TELEGRAM ---
    msg = f"🎯 PRONOS TIRS & xG (Précision Max)\n📅 {datetime.now().strftime('%d/%m/%Y')}\n"
    msg += f"🔎 Matchs analysés : {matchs_analyses}\n\n"
    
    if picks_total:
        msg += "📈 TIRS TOTAUX :\n" + "\n\n".join(picks_total[:3]) + "\n\n"
    if picks_cadres_eq:
        msg += "🎯 TIRS CADRÉS (Équipes) :\n" + "\n\n".join(picks_cadres_eq[:3]) + "\n\n"
    if picks_xg:
        msg += "🧠 ANALYSE xG (Qualité des occasions) :\n" + "\n\n".join(picks_xg[:3]) + "\n\n"
    if picks_cadres_joueurs:
        msg += "👤 TIRS CADRÉS (Joueurs) :\n" + "\n\n".join(picks_cadres_joueurs[:3]) + "\n\n"
        
    if not any([picks_total, picks_cadres_eq, picks_xg, picks_cadres_joueurs]):
        msg += "❌ Aucune sélection ne passe les filtres de sécurité aujourd'hui."
        
    send_telegram(msg)
    print("✅ Message envoyé !")

if __name__ == "__main__":
    main()
