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
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"): return {}
    
    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("date", ""), reverse=True)
    
    player_history = {}
    for e in finished[:5]:
        stats = api_get(f"events/{e['id']}/player-stats/")
        if stats and "player_stats" in stats:
            for p in stats["player_stats"]:
                if p.get("team_id") == team_id and p.get("minutes_played", 0) >= 45:
                    name = p.get("player_name", "Inconnu")
                    if name not in player_history: player_history[name] = []
                    player_history[name].append(p.get("shots_on_target", 0))
    return player_history

def main():
    print("⏳ Analyse de diagnostic...")
    today = datetime.now().strftime("%Y-%m-%d")
    
    events = api_get(f"events/?date_from={today}&date_to={today}")
    if not events or not events.get("results"):
        send_telegram("🤷‍♂️ Aucun match aujourd'hui.")
        return
    
    matches = events["results"]
    
    best_total = []
    best_eq = []
    best_joueur = []
    
    matchs_analyses = 0
    
    for m in matches:
        if m.get("status") != "scheduled": continue
        matchs_analyses += 1
        
        home_name = m.get("home_team")
        away_name = m.get("away_team")
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        
        # 1. TIRS TOTAUX
        h_total = get_team_stats(home_id, "total_shots")
        a_total = get_team_stats(away_id, "total_shots")
        if len(h_total) >= 3 and len(a_total) >= 3:
            match_total = (sum(h_total)/len(h_total)) + (sum(a_total)/len(a_total))
            p_275 = poisson_plus_de(match_total, 27.5)
            best_total.append((p_275, f"{home_name}—{away_name} (Moy: {match_total:.0f})"))

        # 2. TIRS CADRÉS ÉQUIPES
        h_sot = get_team_stats(home_id, "shots_on_target")
        a_sot = get_team_stats(away_id, "shots_on_target")
        if len(h_sot) >= 3:
            p_h = poisson_plus_de(sum(h_sot)/len(h_sot), 3.5)
            best_eq.append((p_h, f"{home_name} +3,5"))
        if len(a_sot) >= 3:
            p_a = poisson_plus_de(sum(a_sot)/len(a_sot), 3.5)
            best_eq.append((p_a, f"{away_name} +3,5"))

        # 3. JOUEURS
        h_players = get_players_sot_history(home_id)
        a_players = get_players_sot_history(away_id)
        for p_name, sots in {**h_players, **a_players}.items():
            if len(sots) >= 3:
                p05 = poisson_plus_de(sum(sots)/len(sots), 0.5)
                best_joueur.append((p05, f"{p_name} +0,5"))
                
    # Tri des meilleurs
    best_total.sort(key=lambda x: -x[0])
    best_eq.sort(key=lambda x: -x[0])
    best_joueur.sort(key=lambda x: -x[0])
    
    msg = f"🔍 DIAGNOSTIC TIRS\n📅 {datetime.now().strftime('%d/%m/%Y')}\n"
    msg += f"Matchs à venir analysés : {matchs_analyses}\n\n"
    
    msg += "📈 Meilleurs TIRS TOTAUX (+27,5) :\n"
    if best_total:
        for p, name in best_total[:3]: msg += f"• {name} : {p:.0%}\n"
    else: msg += "• Pas de données.\n"
    
    msg += "\n🎯 Meilleurs TIRS CADRÉS (+3,5) :\n"
    if best_eq:
        for p, name in best_eq[:3]: msg += f"• {name} : {p:.0%}\n"
    else: msg += "• Pas de données.\n"
    
    msg += "\n👤 Meilleurs JOUEURS (+0,5) :\n"
    if best_joueur:
        for p, name in best_joueur[:3]: msg += f"• {name} : {p:.0%}\n"
    else: msg += "• Pas de données.\n"
    
    send_telegram(msg)
    print("✅ Diagnostic envoyé !")

if __name__ == "__main__":
    main()
