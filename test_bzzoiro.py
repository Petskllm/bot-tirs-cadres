import os
import requests
import json
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
    # Telegram limite à 4096 caractères, on coupe si besoin
    for i in range(0, len(msg), 4000):
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg[i:i+4000]})

def main():
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    events = api_get(f"events/?date_from={tomorrow}&date_to={tomorrow}")
    
    if not events or not events.get("results"):
        send_telegram("Aucun match trouvé pour demain.")
        return
    
    matches = events["results"]
    
    # On prend le premier match pour voir sa structure exacte
    first_match = matches[0]
    
    # On extrait les clés principales (ce qui pourrait contenir le nom de la ligue)
    keys_to_check = ["league_name", "league", "competition", "league_id", "country"]
    extracted_info = {}
    for key in keys_to_check:
        if key in first_match:
            extracted_info[key] = first_match[key]
            
    # On prépare un message lisible
    msg = f"🕵️ RECHERCHE DU NOM DE LA LIGUE (Match de demain)\n\n"
    msg += f"⚽ Match : {first_match.get('home_team')} vs {first_match.get('away_team')}\n\n"
    msg += f"📋 Infos trouvées :\n{json.dumps(extracted_info, indent=2, ensure_ascii=False)}\n\n"
    msg += f"🔑 TOUTES les clés du match :\n{list(first_match.keys())}"
    
    send_telegram(msg)
    print("✅ Diagnostic envoyé !")

if __name__ == "__main__":
    main()
