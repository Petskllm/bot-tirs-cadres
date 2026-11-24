"""Analyse post-match API-Football uniquement, sans Bzzoiro ni Telegram."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

import requests

BASE = "https://v3.football.api-sports.io"
FIELDS = {
    "total_shots": ("Total Shots",),
    "shots_on_target": ("Shots on Goal", "Shots on Target"),
    "corners": ("Corner Kicks",),
    "yellow_cards": ("Yellow Cards",),
    "red_cards": ("Red Cards",),
}


def get(path, token, params):
    response = requests.get(
        f"{BASE}/{path}",
        headers={"x-apisports-key": token, "Accept": "application/json"},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload.get("response", [])


def extract(statistics, team_name):
    values = {}
    for team in statistics:
        if team.get("team", {}).get("name") != team_name:
            continue
        raw = {item.get("type"): item.get("value") for item in team.get("statistics", [])}
        for field, names in FIELDS.items():
            values[field] = next((raw[name] for name in names if raw.get(name) is not None), None)
    return values


def run(date, max_matches):
    token = os.getenv("API_FOOTBALL_KEY")
    if not token:
        return {"status": "unavailable", "error": "API_FOOTBALL_KEY absente"}
    fixtures = get("fixtures", token, {"date": date})
    finished = [item for item in fixtures if item.get("fixture", {}).get("status", {}).get("short") in {"FT", "AET", "PEN"}]
    selected = finished[:max_matches]
    report = {"status": "ok", "date": date, "fixtures_found": len(fixtures), "finished_sample": len(selected), "matches": []}
    for item in selected:
        fixture = item.get("fixture", {})
        teams = item.get("teams", {})
        home = teams.get("home", {}).get("name", "")
        away = teams.get("away", {}).get("name", "")
        statistics = get("fixtures/statistics", token, {"fixture": fixture.get("id")})
        report["matches"].append({
            "fixture_id": fixture.get("id"),
            "match": f"{home} — {away}",
            "status": fixture.get("status", {}).get("short"),
            "home": extract(statistics, home),
            "away": extract(statistics, away),
        })
        for side in ("home", "away"):
            yellow = report["matches"][-1][side].get("yellow_cards")
            red = report["matches"][-1][side].get("red_cards")
            if isinstance(yellow, (int, float)) and isinstance(red, (int, float)):
                report["matches"][-1][side]["total_cards"] = yellow + red
    report["coverage"] = {
        field: sum(
            any(match[side].get(field) is not None for side in ("home", "away"))
            for match in report["matches"]
        )
        for field in FIELDS
    }
    report["coverage"]["total_cards"] = sum(
        any(match[side].get("total_cards") is not None for side in ("home", "away"))
        for match in report["matches"]
    )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--max-matches", type=int, default=5)
    args = parser.parse_args()
    try:
        report = run(args.date, max(1, min(args.max_matches, 5)))
    except requests.HTTPError as exc:
        report = {"status": "unavailable", "error": f"HTTP {exc.response.status_code if exc.response else 'unknown'}"}
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        report = {"status": "unavailable", "error": type(exc).__name__}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
