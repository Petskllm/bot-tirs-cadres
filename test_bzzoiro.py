import os
import requests
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

def send_telegram(msg):
    for i in range(0, len(msg), 4000):
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg[i:i+4000]})

def main():
    # On regarde les matchs de DEMAIN pour être sûr d'avoir le statut "notstarted"
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    events = api_get(f"events/?date_from={tomorrow}&date_to={tomorrow}")
    
    if not events or not events.get("results"):
        send_telegram(f"Aucun match trouvé dans l'API pour demain ({tomorrow}).")
        return
    
    matches = events["results"]
    leagues = set()
    statuses = set()
    
    for m in matches:
        name = m.get("league_name")
        status = m.get("status")
        if name: leagues.add(name)
        if status: statuses.add(status)
                
    msg = f"🔍 LIGUES DE DEMAIN ({tomorrow}) :\n\n"
    if leagues:
        msg += "\n".join(f"• {name}" for name in sorted(leagues))
    else:
        msg += "Aucune ligue trouvée."
        
    msg += f"\n\n📊 Statuts rencontrés : {list(statuses)}"
        
    send_telegram(msg)
    print("✅ Liste envoyée !")

if __name__ == "__main__":
    main()
