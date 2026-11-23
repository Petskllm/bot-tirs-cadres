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
    print("❌ Secrets manquants (BZZOIRO_TOKEN, TELEGRAM_TOKEN, CHAT_ID)")
    exit(1)

def api_get(endpoint):
    try:
        r = requests.get(f"{BASE}/{endpoint}", headers=headers, timeout=15)
        return r.json() if r.status_code == 200 else None
    except:
        return None

def poisson_plus_de(lam, k):
    """Probabilité de faire PLUS de k (ex: k=2.5 -> 3 ou plus)"""
    if lam <= 0: return 0.0
    p = 0
    for i in range(int(k) + 1):
        p += math.exp(-lam) * (lam ** i) / math.factorial(i)
    return 1 - p

def send_telegram(msg):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": msg})

def get_team_sot_history(team_id):
    """Moyenne des tirs cadrés sur les 5 derniers matchs (équipe)."""
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"): return []
    
    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("date", ""), reverse=True)
    
    sots = []
    for e in finished[:5]:
        st = api_get(f"events/{e['id']}/stats/")
        if st and st.get("stats"):
            is_home = (e["home_team_id"] == team_id)
            side = "home" if is_home else "away"
            val = st["stats"].get(side, {}).get("shots_on_target")
            if val is not None:
                sots.append(val)
    return sots

def get_players_sot_history(team_id):
    """Moyenne des tirs cadrés des joueurs sur le dernier match."""
    today = datetime.now().strftime("%Y-%m-%d")
    past = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    events = api_get(f"events/?team_id={team_id}&date_from={past}&date_to={today}")
    if not events or not events.get("results"): return {}

    # On prend le dernier match de l'équipe
    finished = [e for e in events["results"] if e.get("status") == "finished"]
    finished.sort(key=lambda x: x.get("date", ""), reverse=True)
    if not finished: return {}
    
    last_match = finished[0]
    stats = api_get(f"events/{last_match['id']}/player-stats/")
    if not stats or "player_stats" not in stats: return {}
    
    # On ne garde que les joueurs qui ont joué au moins 60 min
    players = {}
    for p in stats["player_stats"]:
        if p.get("minutes_played", 0) >= 60 and p.get("team_id") == team_id:
            sots = p.get("shots_on_target", 0)
            name = p.get("player_name", "Joueur inconnu")
            players[name] = sots
            
    return players

def main():
    print("⏳ Analyse des matchs du jour (Équipes & Joueurs)...")
    today = datetime.now().strftime("%Y-%m-%d")
    
    events = api_get(f"events/?date_from={today}&date_to={today}")
    if not events or not events.get("results"):
        print("Aucun match aujourd'hui.")
        return
    
    matches = events["results"]
    print(f"✅ {len(matches)} matchs trouvés.")
    
    team_picks = []
    player_picks = []
    
    for m in matches:
        if m.get("status") != "scheduled": continue
            
        home_name = m.get("home_team")
        away_name = m.get("away_team")
        home_id = m.get("home_team_id")
        away_id = m.get("away_team_id")
        
        # --- 1. ANALYSE ÉQUIPES ---
        h_sots = get_team_sot_history(home_id)
        a_sots = get_team_sot_history(away_id)
        
        if len(h_sots) >= 3:
            h_avg = sum(h_sots) / len(h_sots)
            prob = poisson_plus_de(h_avg, 2.5)
            if prob >= 0.70:
                team_picks.append(f"• {home_name} : +2,5 tirs cadrés ({prob:.0%})")
                
        if len(a_sots) >= 3:
            a_avg = sum(a_sots) / len(a_sots)
            prob = poisson_plus_de(a_avg, 2.5)
            if prob >= 0.70:
                team_picks.append(f"• {away_name} : +2,5 tirs cadrés ({prob:.0%})")

        # --- 2. ANALYSE JOUEURS ---
        # On récupère les joueurs du dernier match de chaque équipe
        h_players = get_players_sot_history(home_id)
        a_players = get_players_sot_history(away_id)
        
        for p_name, p_sots in h_players.items():
            # Si le joueur a fait au moins 1 tir cadré au dernier match
            # On l'affiche avec une note indicative
            if p_sots >= 1:
                player_picks.append(f"• {p_name} ({home_name}) : +0,5 tir cadré")
                
        for p_name, p_sots in a_players.items():
            if p_sots >= 1:
                player_picks.append(f"• {p_name} ({away_name}) : +0,5 tir cadré")

    # --- 3. ENVOI DU MESSAGE ---
    msg = f"🎯 PRONOS TIRS CADRÉS\n📅 {datetime.now().strftime('%d/%m/%Y')}\n\n"
    
    if team_picks:
        msg += "🏆 ÉQUIPES :\n" + "\n".join(team_picks[:10]) + "\n\n"
    if player_picks:
        msg += "👤 JOUEURS (Dernier match) :\n" + "\n".join(player_picks[:10]) + "\n\n"
        
    if team_picks or player_picks:
        send_telegram(msg)
        print("✅ Message envoyé !")
    else:
        print("❌ Aucun prono fiable aujourd'hui.")

if __name__ == "__main__":
    main()
