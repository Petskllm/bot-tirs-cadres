"""Compare la couverture des statistiques tirs/corners entre deux APIs.

Mode diagnostic uniquement : aucune sélection, aucun calcul de probabilité,
aucun envoi Telegram et aucune écriture dans data/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

BZZ_BASE = "https://sports.bzzoiro.com/api/v2"
API_BASE = "https://v3.football.api-sports.io"


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def get_json(url, *, headers=None, params=None):
    response = requests.get(url, headers=headers, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload


def value_present(value):
    return value is not None and value != "" and value != "-"


def bzz_stat(stats, side, *names):
    values = stats.get("stats", {}).get(side, {}) if isinstance(stats, dict) else {}
    for name in names:
        if name in values and value_present(values[name]):
            return values[name]
    return None


def api_stat(stats, side_name, *types):
    for team in stats if isinstance(stats, list) else []:
        if team.get("team", {}).get("name") != side_name:
            continue
        values = {str(item.get("type", "")).lower(): item.get("value")
                  for item in team.get("statistics", [])}
        for stat_type in types:
            value = values.get(stat_type.lower())
            if value_present(value):
                return value
    return None


def compare(date, max_matches=5):
    bzz_token = os.getenv("BZZOIRO_TOKEN")
    api_token = os.getenv("API_FOOTBALL_KEY")
    result = {"date": date, "max_matches": max_matches, "matches": [], "summary": {}}
    if not bzz_token or not api_token:
        result["error"] = "secret manquant (BZZOIRO_TOKEN ou API_FOOTBALL_KEY)"
        return result

    bzz_headers = {"Authorization": f"Token {bzz_token}"}
    api_headers = {"x-apisports-key": api_token, "Accept": "application/json"}
    bzz_events = get_json(
        f"{BZZ_BASE}/events/",
        headers=bzz_headers,
        params={"date_from": date, "date_to": date, "limit": 200},
    )
    events = [event for event in bzz_events.get("results", [])
              if event.get("status") == "finished"]
    events.sort(key=lambda event: event.get("event_date", ""), reverse=True)

    api_payload = get_json(f"{API_BASE}/fixtures", headers=api_headers, params={"date": date})
    fixtures = api_payload.get("response", [])
    by_pair = {
        (normalize(item.get("teams", {}).get("home", {}).get("name")),
         normalize(item.get("teams", {}).get("away", {}).get("name"))): item
        for item in fixtures
    }

    for event in events:
        if len(result["matches"]) >= max_matches:
            break
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        item = {
            "bzz_event_id": event.get("id"),
            "match": f"{home} — {away}",
            "bzzoiro": {"matched": True},
            "api_football": {"matched": False},
        }
        bzz_stats = get_json(f"{BZZ_BASE}/events/{event['id']}/stats/", headers=bzz_headers)
        bzz_values = {}
        for field, names in {
            "total_shots": ("total_shots", "shots"),
            "shots_on_target": ("shots_on_target", "shots_on_goal"),
            "corners": ("corner_kicks", "corners"),
        }.items():
            bzz_values[field] = {
                "home": bzz_stat(bzz_stats, "home", *names),
                "away": bzz_stat(bzz_stats, "away", *names),
            }
        item["bzzoiro"]["fields"] = {
            field: any(value_present(value) for value in values.values())
            for field, values in bzz_values.items()
        }

        fixture = by_pair.get((normalize(home), normalize(away)))
        if fixture:
            fixture_id = fixture.get("fixture", {}).get("id")
            item["api_football"].update({"matched": True, "fixture_id": fixture_id})
            api_stats = get_json(
                f"{API_BASE}/fixtures/statistics",
                headers=api_headers,
                params={"fixture": fixture_id},
            ).get("response", [])
            api_home = fixture.get("teams", {}).get("home", {}).get("name", home)
            api_away = fixture.get("teams", {}).get("away", {}).get("name", away)
            api_values = {
                "total_shots": ("total shots",),
                "shots_on_target": ("shots on goal", "shots on target"),
                "corners": ("corner kicks",),
            }
            item["api_football"]["fields"] = {
                field: any(value_present(api_stat(api_stats, side, *types))
                           for side in (api_home, api_away))
                for field, types in api_values.items()
            }
        result["matches"].append(item)

    fields = ("total_shots", "shots_on_target", "corners")
    for source in ("bzzoiro", "api_football"):
        result["summary"][source] = {
            "matched_matches": sum(item[source]["matched"] for item in result["matches"]),
            "field_coverage": {
                field: sum(item[source].get("fields", {}).get(field, False)
                           for item in result["matches"])
                for field in fields
            },
        }
    result["summary"]["compared_matches"] = len(result["matches"])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--max-matches", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(compare(args.date, max(1, min(args.max_matches, 5))), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
