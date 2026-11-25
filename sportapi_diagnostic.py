#!/usr/bin/env python3
"""Read-only SportAPI.ai coverage audit for the bot's football competitions."""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://sportapi.ai/api"
API_KEY = os.environ.get("SPORTAPI_KEY", "").strip()
USER_AGENT = "bot-tirs-cadres-sportapi-readonly-audit/1.0"
COMPLETED_STATUSES = {"FT", "AET", "PENS", "finished", "Finished"}
LOOKBACK_DAYS = 7
MAX_STATS_PROBES = 20

BOT_ALLOWED_LEAGUE_NAMES = {
    "premier league", "la liga", "laliga", "serie a", "bundesliga", "ligue 1",
    "champions league", "europa league", "conference league", "eredivisie",
    "primeira liga", "championship", "brasileirao", "ligue 2", "serie b",
    "la liga 2",
}

# Exact catalogue names and country context avoid counting similarly named leagues
# (for example the Nigerian Premier League) as the bot's target competitions.
TARGETS = [
    ("Premier League", {"premier league"}, {"england"}),
    ("La Liga", {"spanish la liga", "la liga"}, {"spain"}),
    ("Serie A - Italy", {"italian serie a", "serie a"}, {"italy"}),
    ("Bundesliga", {"german bundesliga", "bundesliga"}, {"germany"}),
    ("Ligue 1", {"french ligue 1", "ligue 1"}, {"france"}),
    ("Champions League", {"uefa champions league"}, set()),
    ("Europa League", {"uefa europa league"}, set()),
    ("Conference League", {"uefa conference league"}, set()),
    ("Eredivisie", {"dutch eredivisie", "eredivisie"}, {"netherlands"}),
    ("Primeira Liga", {"portuguese primeira liga", "primeira liga"}, {"portugal"}),
    ("Championship", {"championship"}, {"england"}),
    ("Brasileirão / Serie A - Brazil", {"brazilian serie a", "brasileirao"}, {"brazil"}),
    ("Ligue 2", {"french ligue 2", "ligue 2"}, {"france"}),
    ("Serie B - Italy", {"italian serie b", "serie b"}, {"italy"}),
    ("Serie B - Brazil", {"brazilian serie b", "serie b"}, {"brazil"}),
    ("La Liga 2", {"spanish la liga 2", "la liga 2"}, {"spain"}),
]


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().replace("-", " ").replace("_", " ").split())


def api_get(path: str):
    """Make one GET request; return status, parsed payload and rate-limit headers."""
    request = Request(
        API_BASE + path,
        headers={"X-Api-Key": API_KEY, "User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return response.status, payload, response.headers
    except HTTPError as exc:
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        return exc.code, payload, exc.headers
    except (URLError, TimeoutError, ValueError):
        return 0, {}, {}


def main() -> int:
    if not API_KEY:
        print("ERROR: secret SPORTAPI_KEY is not available to this workflow.")
        return 2

    status, payload, headers = api_get("/leagues")
    if status != 200 or payload.get("success") is not True:
        print(f"ERROR: GET /leagues returned HTTP {status}; key value was not logged.")
        return 1

    groups = payload.get("data", [])
    catalogue = []
    for group in groups if isinstance(groups, list) else []:
        country = str(group.get("country", "")) if isinstance(group, dict) else ""
        rows = group.get("leagues", []) if isinstance(group, dict) else []
        for league in rows if isinstance(rows, list) else []:
            if not isinstance(league, dict):
                continue
            name = str(league.get("name", ""))
            norm_name, norm_country = normalize(name), normalize(country)
            target_label = None
            for label, aliases, countries in TARGETS:
                if norm_name in aliases and (not countries or norm_country in countries):
                    target_label = label
                    break
            if target_label:
                catalogue.append({
                    "target": target_label,
                    "id": league.get("id"),
                    "name": name,
                    "country": country,
                    "has_fixtures": league.get("has_fixtures"),
                    "fixtures_count": league.get("fixtures_count"),
                    "bot_exact_name_match": normalize(name) in BOT_ALLOWED_LEAGUE_NAMES,
                })

    print(f"GET /leagues: HTTP 200; catalog groups={len(groups)}; target competitions found={len(catalogue)}")
    for row in catalogue:
        print(
            "LEAGUE " + json.dumps(row, ensure_ascii=False, sort_keys=True)
        )

    catalogue_by_id = {str(row["id"]): row for row in catalogue if row.get("id") is not None}
    today = datetime.now(timezone.utc).date()
    fixtures_by_id = {}
    day_summary = []
    for offset in range(LOOKBACK_DAYS + 1):
        day = today - timedelta(days=offset)
        path = f"/fixtures/date/{day.isoformat()}"
        day_status, day_payload, day_headers = api_get(path)
        if day_status != 200 or day_payload.get("success") is not True:
            day_summary.append((day.isoformat(), day_status, None, None))
            continue
        fixtures = day_payload.get("data", [])
        relevant = [
            fixture for fixture in fixtures if isinstance(fixture, dict)
            and str(fixture.get("league_id")) in catalogue_by_id
        ]
        day_summary.append((day.isoformat(), day_status, len(fixtures), len(relevant)))
        for fixture in relevant:
            fixture_id = fixture.get("id")
            if fixture_id is None:
                continue
            if str(fixture.get("status", "")) in COMPLETED_STATUSES:
                key = str(fixture.get("league_id"))
                old = fixtures_by_id.get(key)
                if old is None or str(fixture.get("date", "")) > str(old.get("date", "")):
                    fixtures_by_id[key] = fixture

    for row in day_summary:
        print(f"FIXTURES date={row[0]} HTTP={row[1]} total={row[2]} target_competitions={row[3]}")

    probes = 0
    for league_id, fixture in sorted(
        fixtures_by_id.items(),
        key=lambda item: (item[1].get("date", ""), item[1].get("league_name", "")),
        reverse=True,
    ):
        if probes >= MAX_STATS_PROBES:
            break
        probes += 1
        fixture_id = fixture.get("id")
        stat_status, stat_payload, stat_headers = api_get(f"/fixtures/{fixture_id}/stats")
        data = stat_payload.get("data", {}) if isinstance(stat_payload, dict) else {}
        home = data.get("home", {}) if isinstance(data, dict) else {}
        away = data.get("away", {}) if isinstance(data, dict) else {}
        required = ("shots", "shots_on_target", "yellow_cards")
        home_present = [field for field in required if isinstance(home, dict) and home.get(field) is not None]
        away_present = [field for field in required if isinstance(away, dict) and away.get(field) is not None]
        print("MATCH_STATS " + json.dumps({
            "target": catalogue_by_id[league_id]["target"],
            "league": fixture.get("league_name"),
            "date": fixture.get("date"),
            "fixture_id": fixture_id,
            "http": stat_status,
            "available": stat_payload.get("available") if isinstance(stat_payload, dict) else None,
            "home_required_fields": home_present,
            "away_required_fields": away_present,
        }, ensure_ascii=False, sort_keys=True))

    covered_ids = set(fixtures_by_id)
    for row in catalogue:
        if str(row.get("id")) not in covered_ids:
            print("NO_RECENT_FINISHED_MATCH " + json.dumps({"target": row["target"], "league": row["name"]}, ensure_ascii=False))

    query = urlencode({"name": "Bukayo Saka", "team_id": 311, "shirt_number": 7})
    player_status, player_payload, player_headers = api_get("/players/stats?" + query)
    matches = player_payload.get("matches", []) if isinstance(player_payload, dict) else []
    match_fields = sorted(matches[0].keys()) if matches and isinstance(matches[0], dict) else []
    individual_sot_present = any(
        isinstance(match, dict) and match.get("shots_on_target") is not None for match in matches
    )
    print("PLAYER_PROBE " + json.dumps({
        "http": player_status,
        "success": player_payload.get("success") if isinstance(player_payload, dict) else None,
        "matches_returned": len(matches),
        "first_match_fields": match_fields,
        "individual_shots_on_target_present": individual_sot_present,
    }, ensure_ascii=False, sort_keys=True))

    remaining = headers.get("X-RateLimit-Remaining") if headers else None
    if player_headers:
        remaining = player_headers.get("X-RateLimit-Remaining", remaining)
    print(f"AUDIT_DONE; requests_remaining_today={remaining if remaining is not None else 'not reported'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
