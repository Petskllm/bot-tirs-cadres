import os
import requests
from datetime import datetime, timedelta

TOKEN = os.getenv("BZZOIRO_TOKEN")
if not TOKEN:
    print("❌ Token manquant")
    exit(1)

headers = {"Authorization": f"Token {TOKEN}"}
BASE = "https://sports.bzzoiro.com/api/v2"

# On regarde HIER pour être sûr que les matchs sont terminés
date_test = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
print(f"🔍 Test pour la date d'hier : {date_test}")

r = requests.get(f"{BASE}/events/?date_from={date_test}&date_to={date_test}", headers=headers)
data = r.json()
events = data.get("results", [])
print(f"✅ Matchs trouvés hier : {len(events)}")

if not events:
    print("Aucun match hier. Change la date.")
    exit()

# On prend le premier match
event_id = events[0]["id"]
print(f"\n--- Stats du match {event_id} : {events[0].get('home_team')} vs {events[0].get('away_team')} ---")

stats_r = requests.get(f"{BASE}/events/{event_id}/stats/", headers=headers)
if stats_r.status_code == 200:
    stats_data = stats_r.json()
    # On vérifie si les tirs cadrés sont là
    home = stats_data.get("stats", {}).get("home", {})
    print(f"Tirs cadrés Domicile : {home.get('shots_on_target')}")
    print(f"Tirs cadrés Extérieur : {stats_data.get('stats', {}).get('away', {}).get('shots_on_target')}")
    print(f"Total tirs Domicile : {home.get('total_shots')}")
else:
    print(f"Stats indisponibles ({stats_r.status_code})")

# Stats joueurs
print(f"\n--- Stats joueurs ---")
player_r = requests.get(f"{BASE}/events/{event_id}/player-stats/", headers=headers)
if player_r.status_code == 200:
    p_data = player_r.json()
    print(f"Nombre de joueurs avec stats : {p_data.get('count', 0)}")
    if p_data.get('count', 0) > 0:
        # On affiche les 3 premiers joueurs pour voir la structure
        for p in p_data.get("player_stats", [])[:3]:
            print(p)
else:
    print(f"Stats joueurs indisponibles ({player_r.status_code})")
