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
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": msg})

def get_team_sot_history(team_id):
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

def main():
    print("⏳ Analyse des matchs...")
    today = datetime.now().strftime("%Y-%m-%d")
    
    events = api_get(f"events/?date_from={today}&date_to={today}")
    if not events or not events.get("results"):
        send_telegram("🤷‍♂️ Aucun match programmé aujourd'hui.")
        return
    
    matches = events["results"]
    team_picks = []
    
    for m in matches:
        if m.get("status") != "scheduled": continue
            
        home_name = m.get("home_team")
        home_id = m.get("home_team_id")
        
        h_sots = get_team_sot_history(home_id)
        if len(h_sots) >= 3:
            h_avg = sum(h_sots) / len(h_sots)
            prob = poisson_plus_de(h_avg, 2.5)
            
            if prob >= 0.60: # Seuil à 60%
                # Calcul de la cote minimale (avec 5% de marge de sécurité)
                cote_min = round(1 / prob * 1.05, 2)
                team_picks.append(f"• {home_name} : +2,5 tirs cadrés ({prob:.0%})\n  💰 Cote min : {cote_min}")

    msg = f"🎯 PRONOS TIRS CADRÉS\n📅 {datetime.now().strftime('%d/%m/%Y')}\n\n"
    
    if team_picks:
        msg += "🏆 SÉLECTIONS (Seuil 60%) :\n" + "\n".join(team_picks[:10])
    else:
        msg += "❌ Aucune équipe n'atteint 60% aujourd'hui."
        
    send_telegram(msg)
    print("✅ Message envoyé !")

if __name__ == "__main__":
    main()
