#!/usr/bin/env python3
"""Read-only 30-day SportAPI.ai coverage audit for target bot competitions."""

from __future__ import annotations

import json
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "https://sportapi.ai/api"
API_KEY = os.environ.get("SPORTAPI_KEY", "").strip()
USER_AGENT = "bot-tirs-cadres-sportapi-30day-readonly-audit/1.0"
LOOKBACK_DAYS = 30  # today plus the preceding 29 calendar dates
MAX_STATS_PROBES = 1000  # preserve a large buffer under the free daily quota
REQUIRED_FIELDS = ("shots", "shots_on_target", "yellow_cards")
COMPLETED_STATUSES = {"ft", "aet", "pens", "finished"}
BOT_ALLOWED_LEAGUE_NAMES = {
    "premier league", "la liga", "laliga", "serie a", "bundesliga", "ligue 1",
    "champions league", "europa league", "conference league", "eredivisie",
    "primeira liga", "championship", "brasileirao", "ligue 2", "serie b", "la liga 2",
}
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

API_CALLS = 0
RATE_REMAINING = []
RATE_LIMITED = False


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().replace("-", " ").replace("_", " ").split())


def api_get(path: str):
    """Issue one GET; do not print request headers, credentials, or raw error bodies."""
    global API_CALLS, RATE_LIMITED
    API_CALLS += 1
    request = Request(
        API_BASE + path,
        headers={"X-Api-Key": API_KEY, "User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=25) as response:
            headers = response.headers
            remaining = headers.get("X-RateLimit-Remaining")
            if remaining and remaining.isdigit():
                RATE_REMAINING.append(int(remaining))
            raw = response.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return response.status, payload
    except HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        if remaining and remaining.isdigit():
            RATE_REMAINING.append(int(remaining))
        if exc.code == 429:
            RATE_LIMITED = True
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        return exc.code, payload
    except (URLError, TimeoutError, ValueError):
        return 0, {}


def make_summary(catalogue_row: dict) -> dict:
    return {
        "catalogue": catalogue_row,
        "fixtures": 0,
        "finished": 0,
        "stats_http_200": 0,
        "available_true": 0,
        "available_false": 0,
        "available_unknown": 0,
        "home_fields_complete": 0,
        "away_fields_complete": 0,
        "both_sides_complete": 0,
        "field_home": {field: 0 for field in REQUIRED_FIELDS},
        "field_away": {field: 0 for field in REQUIRED_FIELDS},
        "http_errors": 0,
        "stats_errors": 0,
        "sample_dates": [],
    }


def write_github_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        print("WARNING: GitHub job summary could not be written.")


def main() -> int:
    if not API_KEY:
        print("ERROR: secret SPORTAPI_KEY is not available to this workflow.")
        return 2

    status, payload = api_get("/leagues")
    if status != 200 or payload.get("success") is not True:
        print(f"ERROR: GET /leagues returned HTTP {status}; key value was not logged.")
        return 1

    catalogue = []
    groups = payload.get("data", [])
    for group in groups if isinstance(groups, list) else []:
        country = str(group.get("country", "")) if isinstance(group, dict) else ""
        rows = group.get("leagues", []) if isinstance(group, dict) else []
        for league in rows if isinstance(rows, list) else []:
            if not isinstance(league, dict):
                continue
            name = str(league.get("name", ""))
            norm_name, norm_country = normalize(name), normalize(country)
            for target, aliases, countries in TARGETS:
                if norm_name in aliases and (not countries or norm_country in countries):
                    catalogue.append({
                        "target": target,
                        "id": str(league.get("id", "")),
                        "name": name,
                        "country": country,
                        "has_fixtures": league.get("has_fixtures"),
                        "bot_exact_name_match": norm_name in BOT_ALLOWED_LEAGUE_NAMES,
                    })
                    break

    by_league_id = {row["id"]: make_summary(row) for row in catalogue if row["id"]}
    print(f"AUDIT_START dates={LOOKBACK_DAYS}; target_competitions={len(by_league_id)}")
    today = datetime.now(timezone.utc).date()
    fixtures_by_id = {league_id: {} for league_id in by_league_id}
    successful_date_calls = 0
    date_errors = 0
    total_target_fixtures = 0

    for offset in range(LOOKBACK_DAYS):
        day = today - timedelta(days=offset)
        day_status, day_payload = api_get(f"/fixtures/date/{day.isoformat()}")
        if day_status != 200 or day_payload.get("success") is not True:
            date_errors += 1
            if RATE_LIMITED:
                break
            continue
        successful_date_calls += 1
        fixtures = day_payload.get("data", [])
        for fixture in fixtures if isinstance(fixtures, list) else []:
            if not isinstance(fixture, dict):
                continue
            league_id = str(fixture.get("league_id", ""))
            if league_id not in by_league_id:
                continue
            fixture_id = str(fixture.get("id", ""))
            if not fixture_id:
                continue
            fixtures_by_id[league_id][fixture_id] = fixture

    for league_id, fixtures in fixtures_by_id.items():
        summary = by_league_id[league_id]
        summary["fixtures"] = len(fixtures)
        completed = [
            fixture for fixture in fixtures.values()
            if normalize(fixture.get("status", "")) in COMPLETED_STATUSES
        ]
        completed.sort(key=lambda row: (str(row.get("date", "")), str(row.get("id", ""))))
        summary["finished"] = len(completed)
        summary["sample_dates"] = sorted({str(row.get("date", "")) for row in completed})
        total_target_fixtures += len(completed)

    fixture_work = []
    for league_id, fixtures in fixtures_by_id.items():
        for fixture in fixtures.values():
            if normalize(fixture.get("status", "")) in COMPLETED_STATUSES:
                fixture_work.append((league_id, fixture))
    fixture_work.sort(key=lambda item: (str(item[1].get("date", "")), str(item[1].get("id", ""))))
    truncated = len(fixture_work) > MAX_STATS_PROBES
    fixture_work = fixture_work[:MAX_STATS_PROBES]

    for index, (league_id, fixture) in enumerate(fixture_work, start=1):
        if RATE_LIMITED:
            break
        if index > 1:
            time.sleep(0.04)  # gentle pacing; the free quota remains the hard cap
        fixture_id = fixture.get("id")
        stat_status, stat_payload = api_get(f"/fixtures/{fixture_id}/stats")
        summary = by_league_id[league_id]
        if stat_status != 200:
            summary["http_errors"] += 1
            if RATE_LIMITED:
                break
            continue
        summary["stats_http_200"] += 1
        if not isinstance(stat_payload, dict) or stat_payload.get("success") is not True:
            summary["stats_errors"] += 1
            continue
        if stat_payload.get("available") is True:
            summary["available_true"] += 1
        elif stat_payload.get("available") is False:
            summary["available_false"] += 1
        else:
            summary["available_unknown"] += 1
        data = stat_payload.get("data", {})
        home = data.get("home", {}) if isinstance(data, dict) else {}
        away = data.get("away", {}) if isinstance(data, dict) else {}
        home_complete = all(isinstance(home, dict) and home.get(field) is not None for field in REQUIRED_FIELDS)
        away_complete = all(isinstance(away, dict) and away.get(field) is not None for field in REQUIRED_FIELDS)
        if home_complete:
            summary["home_fields_complete"] += 1
        if away_complete:
            summary["away_fields_complete"] += 1
        if home_complete and away_complete:
            summary["both_sides_complete"] += 1
        for field in REQUIRED_FIELDS:
            if isinstance(home, dict) and home.get(field) is not None:
                summary["field_home"][field] += 1
            if isinstance(away, dict) and away.get(field) is not None:
                summary["field_away"][field] += 1

    processed_any = sum(
        row["stats_http_200"] + row["http_errors"] for row in by_league_id.values()
    )
    title = f"## SportAPI.ai — couverture sur {LOOKBACK_DAYS} jours\n\n"
    title += (
        f"Dates analysées : {today - timedelta(days=LOOKBACK_DAYS - 1)} → {today} UTC. "
        f"Fixtures terminées dans les ligues cibles : {total_target_fixtures}. "
        f"Requêtes API exécutées : {API_CALLS}; quota restant observé : "
        f"{min(RATE_REMAINING) if RATE_REMAINING else 'non communiqué'}.\n\n"
    )
    title += "| Ligue | Pays | Fixtures | Terminées | Stats complètes | Couverture | `available=false` | Erreurs | Filtre bot exact |\n"
    title += "|---|---|---:|---:|---:|---:|---:|---:|:---:|\n"
    for summary in sorted(by_league_id.values(), key=lambda row: row["catalogue"]["target"]):
        meta = summary["catalogue"]
        coverage = 100 * summary["both_sides_complete"] / summary["finished"] if summary["finished"] else 0.0
        title += (
            f"| {meta['name']} | {meta['country']} | {summary['fixtures']} | {summary['finished']} "
            f"| {summary['both_sides_complete']} | {coverage:.1f}% | {summary['available_false']} "
            f"| {summary['http_errors'] + summary['stats_errors']} | "
            f"{'oui' if meta['bot_exact_name_match'] else 'non'} |\n"
        )
    if truncated:
        title += f"\n**Attention :** seuls les {MAX_STATS_PROBES} premiers matchs terminés ont été interrogés; l’audit est tronqué.\n"
    if RATE_LIMITED:
        title += "\n**Attention :** le quota journalier a été atteint; l’audit s’est arrêté avant la fin.\n"
    title += (
        f"\nAppels calendriers réussis : {successful_date_calls}/{LOOKBACK_DAYS}; erreurs calendrier : {date_errors}. "
        f"Vérification joueur SOT : non relancée dans cet audit; le test précédent n’avait pas trouvé ce champ.\n"
    )

    print(title)
    for summary in sorted(by_league_id.values(), key=lambda row: row["catalogue"]["target"]):
        meta = summary["catalogue"]
        print("LEAGUE_RESULT " + json.dumps({
            "target": meta["target"],
            "league": meta["name"],
            "country": meta["country"],
            "fixtures_30d": summary["fixtures"],
            "finished_30d": summary["finished"],
            "stats_complete_both_teams": summary["both_sides_complete"],
            "available_true": summary["available_true"],
            "available_false": summary["available_false"],
            "http_errors": summary["http_errors"],
            "stats_errors": summary["stats_errors"],
            "field_home": summary["field_home"],
            "field_away": summary["field_away"],
            "bot_exact_name_match": meta["bot_exact_name_match"],
        }, ensure_ascii=False, sort_keys=True))
    print(
        f"AUDIT_DONE calls={API_CALLS}; completed_fixtures_found={total_target_fixtures}; "
        f"stats_probes_processed={processed_any}; truncated={truncated}; "
        f"rate_limited={RATE_LIMITED}; requests_remaining_today="
        f"{min(RATE_REMAINING) if RATE_REMAINING else 'not reported'}"
    )
    write_github_summary(title)
    return 0 if not RATE_LIMITED else 3


if __name__ == "__main__":
    sys.exit(main())
