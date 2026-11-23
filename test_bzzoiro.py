import os
import requests
import math
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
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"): return []
    
    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("date", ""), reverse=True)
    
    vals = []
    for e in finished[:5]:
        st = api_get(f"events/{e['id']}/stats/")
        if st and st.get("stats"):
            is_home = (e["home_team_id"] == team_id)
            side = "home" if is_home else "away"
            val = st["stats"].get(side, {}).get(stat_type)
            if val is not None:
                vals.append(val)
    return vals

def get_players_sot_history(team_id):
    """Récupère l'historique des tirs cadrés des joueurs sur les 5 derniers matchs."""
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"): return {}
    
    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("date", ""), reverse=True)
    
    player_history = {} # Dictionnaire {nom_joueur: [liste_des_sots]}
    
    # On regarde les 5 derniers matchs de l'équipe
    for e in finished[:5]:
        stats = api_get(f"events/{e['id']}/player-stats/")
        if stats and "player_stats" in stats:
            for p in stats["player_stats"]:
                # On garde les joueurs de l'équipe qui ont joué au moins 45 min
                if p.get("team_id") == team_id and p.get("minutes_played", 0) >= 45:
                    name = p.get("player_name", "Inconnu")
                    sots = p.get("shots_on_target", 0)
                    if name not in player_history:
                        player_history[name] = []
                    player_history[name].append(sots)
    return player_history

def main():
    print("⏳ Analyse globale en cours (ça peut prendre 2-3 min)...")
    today = datetime.now().strftime("%Y-%m-%d")
    
    events = api_get(f"events/?date_from={today}&date_to={today}")
    if not events or not events.get("results"):
        send_telegram("🤷‍♂️ Aucun match programmé aujourd'hui.")
        return
    
    matches = events["results"]
    
    picks_total = []
    picks_cadres_eq = []
    picks_cadres_joueurs = []
    
    for m in matches:
        if m.get("status") != "scheduled": continue
            
        home_name = m.get("home_team")
        away_name = m.get("away_team")
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        
        # --- 1. TIRS TOTAUX ---
        h_total = get_team_stats(home_id, "total_shots")
        a_total = get_team_stats(away_id, "total_shots")
        if len(h_total) >= 3 and len(a_total) >= 3:
            match_total = (sum(h_total)/len(h_total)) + (sum(a_total)/len(a_total))
            lines_tot = []
            for line in [27.5, 28.5, 29.5]:
                prob = poisson_plus_de(match_total, line)
                if prob >= 0.60:
                    lines_tot.append(f"  • +{line} tirs : {prob:.0%} (Cote min : {round(1/prob*1.05, 2)})")
            if lines_tot:
                picks_total.append(f"⚽ {home_name} — {away_name}\n  Total attendu : {match_total:.0f} tirs\n" + "\n".join(lines_tot))

        # --- 2. TIRS CADRÉS ÉQUIPES ---
        h_sot = get_team_stats(home_id, "shots_on_target")
        a_sot = get_team_stats(away_id, "shots_on_target")
        if len(h_sot) >= 3 and len(a_sot) >= 3:
            h_avg_sot, a_avg_sot = sum(h_sot)/len(h_sot), sum(a_sot)/len(a_sot)
            lines_eq = []
            for line in [3.5, 4.5]:
                p_h, p_a = poisson_plus_de(h_avg_sot, line), poisson_plus_de(a_avg_sot, line)
                if p_h >= 0.58: lines_eq.append(f"  • {home_name} +{line} : {p_h:.0%}")
                if p_a >= 0.58: lines_eq.append(f"  • {away_name} +{line} : {p_a:.0%}")
            if lines_eq:
                picks_cadres_eq.append(f"⚽ {home_name} — {away_name}\n" + "\n".join(lines_eq))

        # --- 3. TIRS CADRÉS JOUEURS ---
        h_players = get_players_sot_history(home_id)
        a_players = get_players_sot_history(away_id)
        
        lines_joueurs = []
        # On fusionne les joueurs des deux équipes
        all_players = {**h_players, **a_players}
        
        for p_name, sots_list in all_players.items():
            if len(sots_list) >= 3: # Au moins 3 matchs joués
                avg_sots = sum(sots_list) / len(sots_list)
                prob_05 = poisson_plus_de(avg_sots, 0.5)
                prob_15 = poisson_plus_de(avg_sots, 1.5)
                
                if prob_05 >= 0.60:
                    cote_min = round(1/prob_05 * 1.05, 2)
                    lines_joueurs.append(f"  • {p_name} +0,5 : {prob_05:.0%} (Cote min : {cote_min})")
                if prob_15 >= 0.50: # Pour le 1,5 on peut descendre un peu le seuil
                    cote_min = round(1/prob_15 * 1.05, 2)
                    lines_joueurs.append(f"  • {p_name} +1,5 : {prob_15:.0%} (Cote min : {cote_min})")
        
        if lines_joueurs:
            # On limite à 5 joueurs par match pour ne pas surcharger
            picks_cadres_joueurs.append(f"⚽ {home_name} — {away_name}\n" + "\n".join(lines_joueurs[:5]))

    # --- 4. CONSTRUCTION DU MESSAGE TELEGRAM ---
    msg = f"🎯 PRONOS TIRS (Bzzoiro)\n📅 {datetime.now().strftime('%d/%m/%Y')}\n\n"
    
    if picks_total:
        msg += "📈 TIRS TOTAUX DANS LE MATCH :\n"
        msg += "\n\n".join(picks_total[:4]) + "\n\n"
        
    if picks_cadres_eq:
        msg += "🎯 TIRS CADRÉS PAR ÉQUIPE :\n"
        msg += "\n\n".join(picks_cadres_eq[:4]) + "\n\n"
        
    if picks_cadres_joueurs:
        msg += "👤 TIRS CADRÉS PAR JOUEUR :\n"
        msg += "\n\n".join(picks_cadres_joueurs[:4]) + "\n\n"
        
    if not picks_total and not picks_cadres_eq and not picks_cadres_joueurs:
        msg += "❌ Aucun match ne remplit les conditions aujourd'hui."
        
    send_telegram(msg)
    print("✅ Message envoyé !")

if __name__ == "__main__":
    main()
