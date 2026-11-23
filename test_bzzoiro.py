import os
import requests

# On récupère le token depuis les secrets GitHub
TOKEN = os.getenv("BZZOIRO_TOKEN")

if not TOKEN:
    print("❌ ERREUR : Le token BZZOIRO_TOKEN n'est pas configuré dans les secrets GitHub.")
    exit(1)

headers = {"Authorization": f"Token {TOKEN}"}
BASE = "https://sports.bzzoiro.com/api/v2"

# On teste avec la date d'aujourd'hui
from datetime import datetime
date_test = datetime.now().strftime("%Y-%m-%d")
print(f"🔍 Test pour la date : {date_test}")

r = requests.get(f"{BASE}/events/?date_from={date_test}&date_to={date_test}", headers=headers)

if r.status_code != 200:
    print(f"❌ Erreur {r.status_code} : {r.text}")
    exit(1)

data = r.json()
events = data.get("results", [])
print(f"✅ Matchs trouvés : {len(events)}")

if not events:
    print("Aucun match ce jour-là. Change la date dans le code.")
    exit()

# On prend le premier match et on regarde ses stats
event_id = events[0]["id"]
print(f"\n--- Stats du match {event_id} : {events[0].get('home_team')} vs {events[0].get('away_team')} ---")

stats_r = requests.get(f"{BASE}/events/{event_id}/stats/", headers=headers)
if stats_r.status_code == 200:
    print(stats_r.json())
else:
    print(f"Stats indisponibles ({stats_r.status_code})")

# On regarde les stats par joueur
print(f"\n--- Stats joueurs du match {event_id} ---")
player_r = requests.get(f"{BASE}/events/{event_id}/player-stats/", headers=headers)
if player_r.status_code == 200:
    print(player_r.json())
else:
    print(f"Stats joueurs indisponibles ({player_r.status_code})")
