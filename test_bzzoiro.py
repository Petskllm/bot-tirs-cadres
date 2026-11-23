import os
import requests
from datetime import datetime

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

def send_telegram(msg):
    for i in range(0, len(msg), 4000):
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg[i:i+4000]})

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    events = api_get(f"events/?date_from={today}&date_to={today}")
    
    if not events or not events.get("results"):
        send_telegram("Aucun match aujourd'hui.")
        return
    
    matches = events["results"]
    leagues = set()
    
    for m in matches:
        if m.get("status") == "notstarted":
            name = m.get("league_name")
            if name:
                leagues.add(name)
                
    msg = f"🔍 LISTE DES LIGUES DU JOUR (API) :\n\n"
    if leagues:
        msg += "\n".join(f"• {name}" for name in sorted(leagues))
    else:
        msg += "Aucune ligue trouvée avec le statut 'notstarted'."
        
    send_telegram(msg)
    print("✅ Liste envoyée !")

if __name__ == "__main__":
    main()
