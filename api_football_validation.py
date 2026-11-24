"""Validation secondaire des matchs Bzzoiro via API-Football.

Cette couche ne modifie jamais les probabilités ni les sélections. Elle sert à
vérifier la correspondance des matchs et la disponibilité des statistiques.
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone

import requests

BASE_URL = "https://v3.football.api-sports.io"


def _normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _api_get(path, params, token):
    response = requests.get(
        f"{BASE_URL}/{path}",
        params=params,
        headers={"x-apisports-key": token, "Accept": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(str(payload["errors"]))
    return payload


def _team_names(fixture):
    teams = fixture.get("teams", {})
    return (
        teams.get("home", {}).get("name", ""),
        teams.get("away", {}).get("name", ""),
    )


def validate_matches(matches, date=None):
    """Compare les matchs Bzzoiro du jour aux fixtures API-Football.

    Une seule requête de fixtures est effectuée. Les statistiques de match ne
    sont pas demandées avant le coup d'envoi et cette fonction ne bloque jamais
    une sélection Bzzoiro.
    """
    token = os.getenv("API_FOOTBALL_KEY")
    if not token:
        return {"enabled": False, "reason": "API_FOOTBALL_KEY absente", "matches": []}

    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        payload = _api_get("fixtures", {"date": date}, token)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        return {"enabled": True, "error": str(exc), "matches": []}

    fixtures = payload.get("response", [])
    by_pair = {}
    for fixture in fixtures:
        home, away = _team_names(fixture)
        by_pair[(_normalize(home), _normalize(away))] = fixture

    results = []
    for match in matches:
        key = (_normalize(match.get("home_team")), _normalize(match.get("away_team")))
        fixture = by_pair.get(key)
        if not fixture:
            results.append({
                "event_id": match.get("id"),
                "match": f"{match.get('home_team')} — {match.get('away_team')}",
                "matched": False,
            })
            continue
        league = fixture.get("league", {})
        results.append({
            "event_id": match.get("id"),
            "match": f"{match.get('home_team')} — {match.get('away_team')}",
            "matched": True,
            "api_fixture_id": fixture.get("fixture", {}).get("id"),
            "status": fixture.get("fixture", {}).get("status", {}).get("short"),
            "league": league.get("name"),
            "season": league.get("season"),
        })

    return {
        "enabled": True,
        "date": date,
        "api_fixtures": len(fixtures),
        "matched": sum(1 for item in results if item["matched"]),
        "total": len(results),
        "matches": results,
    }


def format_report(report):
    if not report.get("enabled"):
        return "🔬 Contrôle API-Football : désactivé (clé absente)"
    if report.get("error"):
        return f"🔬 Contrôle API-Football : erreur non bloquante ({report['error']})"
    return (
        "🔬 Contrôle API-Football : "
        f"{report.get('matched', 0)}/{report.get('total', 0)} matchs correspondants "
        f"({report.get('api_fixtures', 0)} fixtures récupérées). "
        "Contrôle informatif, aucune sélection modifiée."
    )
