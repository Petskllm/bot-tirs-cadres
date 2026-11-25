#!/usr/bin/env python3
"""Read-only head-to-head comparison of Bzzoiro and SportAPI team match stats."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SPORT_BASE = "https://sportapi.ai/api"
BZZ_BASE = "https://sports.bzzoiro.com/api/v2"
SPORT_KEY = os.environ.get("SPORTAPI_KEY", "").strip()
BZZ_TOKEN = os.environ.get("BZZOIRO_TOKEN", "").strip()
DAYS = 30
MAX_COMMON_STATS = 600
FIELDS = ("shots", "shots_on_target", "yellow_cards")
FINISHED = {"ft", "aet", "pens", "finished"}

# Shared canonical targets for the competitions used by the bot.
TARGETS = [
    ("Premier League", {"premier league"}, {"england", "united kingdom", "uk"}),
    ("La Liga", {"spanish la liga", "la liga"}, {"spain"}),
    ("Serie A - Italy", {"italian serie a", "serie a"}, {"italy"}),
    ("Bundesliga", {"german bundesliga", "bundesliga"}, {"germany"}),
    ("Ligue 1", {"french ligue 1", "ligue 1"}, {"france"}),
    ("Champions League", {"uefa champions league", "champions league"}, set()),
    ("Europa League", {"uefa europa league", "europa league"}, set()),
    ("Conference League", {"uefa conference league", "conference league"}, set()),
    ("Eredivisie", {"dutch eredivisie", "eredivisie"}, {"netherlands", "holland"}),
    ("Primeira Liga", {"portuguese primeira liga", "primeira liga"}, {"portugal"}),
    ("Championship", {"championship"}, {"england", "united kingdom", "uk"}),
    ("Brasileirão / Serie A - Brazil", {"brazilian serie a", "brasileirao", "serie a"}, {"brazil"}),
    ("Ligue 2", {"french ligue 2", "ligue 2"}, {"france"}),
    ("Serie B - Italy", {"italian serie b", "serie b"}, {"italy"}),
    ("Serie B - Brazil", {"brazilian serie b", "serie b"}, {"brazil"}),
    ("La Liga 2", {"spanish la liga 2", "la liga 2"}, {"spain"}),
]
TARGET_BY_NAME = {name: (aliases, countries) for name, aliases, countries in TARGETS}

CALLS = {"sport": 0, "bzz": 0}
REMAINING = []
RATE_LIMITED = {"sport": False, "bzz": False}
LAST_CALL = {"sport": 0.0, "bzz": 0.0}


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def country_value(row: dict, parent: str = "") -> str:
    for key in ("country", "country_name", "countryName", "region"):
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("name", value.get("country", ""))
        if value:
            return str(value)
    return parent


def target_for(name: object, country: object = "") -> str | None:
    league_name, league_country = normalize(name), normalize(country)
    matches = []
    for target, aliases, countries in TARGETS:
        if league_name not in aliases:
            continue
        # Distinguish similarly named Serie A/B competitions by country.
        if countries and league_country and league_country not in countries:
            continue
        if league_name in {"serie a", "serie b"} and not league_country:
            continue
        matches.append(target)
    return matches[0] if len(matches) == 1 else None


def api_get(provider: str, base: str, path: str, token: str, auth_header: str):
    if RATE_LIMITED[provider]:
        return 0, {}
    delay = 0.18 if provider == "bzz" else 0.08
    wait = delay - (time.monotonic() - LAST_CALL[provider])
    if wait > 0:
        time.sleep(wait)
    CALLS[provider] += 1
    LAST_CALL[provider] = time.monotonic()
    request = Request(
        f"{base}{path}",
        headers={auth_header: token, "Accept": "application/json", "User-Agent": "readonly-football-source-comparison/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=25) as response:
            remaining = response.headers.get("X-RateLimit-Remaining")
            if provider == "sport" and remaining and remaining.isdigit():
                REMAINING.append(int(remaining))
            body = response.read()
            return response.status, json.loads(body.decode("utf-8")) if body else {}
    except HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        if provider == "sport" and remaining and remaining.isdigit():
            REMAINING.append(int(remaining))
        if exc.code == 429:
            RATE_LIMITED[provider] = True
        return exc.code, {}
    except (URLError, TimeoutError, ValueError):
        return 0, {}


def rows(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "leagues", "fixtures"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("results", "leagues", "fixtures"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    return []


def league_catalog_sport():
    status, payload = api_get("sport", SPORT_BASE, "/leagues", SPORT_KEY, "X-Api-Key")
    if status != 200 or not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError(f"SportAPI GET /leagues failed with HTTP {status}")
    result = {}
    for group in rows(payload):
        if not isinstance(group, dict):
            continue
        country = country_value(group)
        league_rows = group.get("leagues", [])
        for league in league_rows if isinstance(league_rows, list) else []:
            if not isinstance(league, dict):
                continue
            target = target_for(league.get("name"), country_value(league, country))
            if target:
                result[str(league.get("id", ""))] = {
                    "target": target, "name": str(league.get("name", "")), "country": country_value(league, country)
                }
    return result


def league_catalog_bzz():
    status, payload = api_bzz("/leagues/")
    if status in (401, 403):
        return status, payload
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"Bzzoiro GET /leagues/ failed with HTTP {status}")
    league_rows = rows(payload)
    result = {}
    for league in league_rows:
        if not isinstance(league, dict):
            continue
        target = target_for(league.get("name"), country_value(league))
        if target:
            result[str(league.get("id", ""))] = {
                "target": target, "name": str(league.get("name", "")), "country": country_value(league)
            }
    return status, result


def api_bzz(path: str):
    # Keep the exact documented auth header format, without logging its value.
    if RATE_LIMITED["bzz"]:
        return 0, {}
    delay = 0.18 - (time.monotonic() - LAST_CALL["bzz"])
    if delay > 0:
        time.sleep(delay)
    CALLS["bzz"] += 1
    LAST_CALL["bzz"] = time.monotonic()
    request = Request(
        f"{BZZ_BASE}{path}",
        headers={"Authorization": f"Token {BZZ_TOKEN}", "Accept": "application/json", "User-Agent": "readonly-football-source-comparison/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=25) as response:
            body = response.read()
            return response.status, json.loads(body.decode("utf-8")) if body else {}
    except HTTPError as exc:
        if exc.code == 429:
            RATE_LIMITED["bzz"] = True
        return exc.code, {}
    except (URLError, TimeoutError, ValueError):
        return 0, {}


def date_part(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("date", value.get("utc", value.get("datetime", "")))
    return str(value or "")[:10]


def team_name(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("name", value.get("team_name", value.get("display_name", "")))
    return str(value or "").strip()


def completed(fixture: dict) -> bool:
    return normalize(fixture.get("status", "")) in FINISHED


def sport_fixtures(league_map: dict[str, dict], start: date, end: date):
    found = {}
    errors = 0
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        status, payload = api_get("sport", SPORT_BASE, f"/fixtures/date/{day.isoformat()}", SPORT_KEY, "X-Api-Key")
        if status != 200 or not isinstance(payload, dict) or payload.get("success") is not True:
            errors += 1
            if RATE_LIMITED["sport"]:
                break
            continue
        for fixture in rows(payload):
            if not isinstance(fixture, dict):
                continue
            league = league_map.get(str(fixture.get("league_id", "")))
            fid = str(fixture.get("id", ""))
            if not league or not fid:
                continue
            if not completed(fixture):
                continue
            found[fid] = {
                "id": fid, "target": league["target"], "league": league["name"],
                "date": date_part(fixture.get("date", fixture.get("event_date"))),
                "home": team_name(fixture.get("home_team", fixture.get("home_name"))),
                "away": team_name(fixture.get("away_team", fixture.get("away_name"))),
            }
    return found, errors


def bzz_fixtures(league_map: dict[str, dict], start: date, end: date):
    found = {}
    errors = 0
    overflow = []
    for league_id, league in league_map.items():
        query = urlencode({"league_id": league_id, "date_from": start.isoformat(), "date_to": end.isoformat(), "limit": 200})
        status, payload = api_bzz(f"/events/?{query}")
        if status != 200 or not isinstance(payload, dict):
            errors += 1
            if RATE_LIMITED["bzz"]:
                break
            continue
        event_rows = rows(payload)
        if len(event_rows) >= 200:
            overflow.append(league["name"])
        for event in event_rows:
            if not isinstance(event, dict) or not completed(event):
                continue
            eid = str(event.get("id", ""))
            if not eid:
                continue
            found[eid] = {
                "id": eid, "target": league["target"], "league": league["name"],
                "date": date_part(event.get("event_date", event.get("date"))),
                "home": team_name(event.get("home_team", event.get("home_name"))),
                "away": team_name(event.get("away_team", event.get("away_name"))),
            }
    return found, errors, overflow


def match_fixtures(sport: dict, bzz: dict):
    index = defaultdict(list)
    for sf in sport.values():
        key = (sf["target"], normalize(sf["home"]), normalize(sf["away"]))
        if sf["date"] and key[1] and key[2]:
            index[key].append(sf)
    pairs = []
    ambiguous = 0
    used_sport = set()
    for bf in bzz.values():
        key = (bf["target"], normalize(bf["home"]), normalize(bf["away"]))
        if not bf["date"] or not key[1] or not key[2]:
            continue
        try:
            bdate = date.fromisoformat(bf["date"])
        except ValueError:
            continue
        candidates = []
        for sf in index.get(key, []):
            if sf["id"] in used_sport:
                continue
            try:
                delta = abs((date.fromisoformat(sf["date"]) - bdate).days)
            except ValueError:
                continue
            if delta <= 1:
                candidates.append((sf, delta))
        if len(candidates) == 1:
            sf, delta = candidates[0]
            used_sport.add(sf["id"])
            pairs.append((sf, bf, delta))
        elif len(candidates) > 1:
            ambiguous += 1
    return pairs, ambiguous


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stats_sport(fixture_id: str):
    status, payload = api_get("sport", SPORT_BASE, f"/fixtures/{fixture_id}/stats", SPORT_KEY, "X-Api-Key")
    if status != 200 or not isinstance(payload, dict) or payload.get("success") is not True:
        return None, status
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return None, status
    result = {}
    for side in ("home", "away"):
        source = data.get(side, {})
        result[side] = {
            "shots": numeric(source.get("shots")) if isinstance(source, dict) else None,
            "shots_on_target": numeric(source.get("shots_on_target")) if isinstance(source, dict) else None,
            "yellow_cards": numeric(source.get("yellow_cards")) if isinstance(source, dict) else None,
        }
    return result, status


def stats_bzz(event_id: str):
    status, payload = api_bzz(f"/events/{event_id}/stats/")
    if status != 200 or not isinstance(payload, dict):
        return None, status
    source_stats = payload.get("stats", {})
    if not isinstance(source_stats, dict):
        return None, status
    result = {}
    for side in ("home", "away"):
        source = source_stats.get(side, {})
        result[side] = {
            "shots": numeric(source.get("total_shots")) if isinstance(source, dict) else None,
            "shots_on_target": numeric(source.get("shots_on_target")) if isinstance(source, dict) else None,
            "yellow_cards": numeric(source.get("yellow_cards")) if isinstance(source, dict) else None,
        }
    return result, status


def complete_both(stats: dict | None) -> bool:
    return bool(stats) and all(stats[side].get(field) is not None for side in ("home", "away") for field in FIELDS)


def main() -> int:
    if not SPORT_KEY or not BZZ_TOKEN:
        print("ERROR: required API secrets are not available; values were not logged.")
        return 2
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=DAYS - 1)
    print(f"COMPARISON_START window={start.isoformat()}..{today.isoformat()} UTC; mode=GET-only")
    try:
        sport_leagues = league_catalog_sport()
        bzz_status, bzz_catalog_result = league_catalog_bzz()
        if bzz_status in (401, 403):
            print(f"ERROR: Bzzoiro GET /leagues/ returned HTTP {bzz_status}; no stats were requested.")
            return 1
        bzz_leagues = bzz_catalog_result
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"LEAGUE_CATALOG sportapi={len(sport_leagues)} bzzoiro={len(bzz_leagues)}")
    sport, sport_calendar_errors = sport_fixtures(sport_leagues, start, today)
    bzz, bzz_event_errors, overflow = bzz_fixtures(bzz_leagues, start, today)
    pairs, ambiguous = match_fixtures(sport, bzz)
    truncated = len(pairs) > MAX_COMMON_STATS
    pairs = pairs[:MAX_COMMON_STATS]
    print(
        f"FIXTURE_COUNTS sportapi_finished={len(sport)} bzzoiro_finished={len(bzz)} "
        f"matched={len(pairs)} ambiguous={ambiguous} sportapi_calendar_errors={sport_calendar_errors} "
        f"bzzoiro_event_errors={bzz_event_errors}"
    )
    if overflow:
        print("WARNING: event limit reached for Bzzoiro leagues: " + ", ".join(sorted(set(overflow))))

    by_league = defaultdict(lambda: {"matches": 0, "sport_complete": 0, "bzz_complete": 0, "bzz_http_errors": 0, "sport_http_errors": 0})
    per_field = {provider: {field: {"both_sides": 0} for field in FIELDS} for provider in ("sport", "bzz")}
    agreements = {field: {"n": 0, "exact": 0, "within1": 0, "absdiff": 0.0} for field in FIELDS}
    total_sport_complete = total_bzz_complete = total_bzz_missing = total_sport_missing = 0
    processed_matches = 0
    for index, (sf, bf, delta) in enumerate(pairs, start=1):
        if RATE_LIMITED["sport"] or RATE_LIMITED["bzz"]:
            break
        ss, s_status = stats_sport(sf["id"])
        if RATE_LIMITED["sport"]:
            break
        bs, b_status = stats_bzz(bf["id"])
        processed_matches += 1
        league_summary = by_league[sf["target"]]
        league_summary["matches"] += 1
        if s_status != 200 or ss is None:
            if s_status != 200:
                league_summary["sport_http_errors"] += 1
            total_sport_missing += 1
        elif not complete_both(ss):
            total_sport_missing += 1
        else:
            total_sport_complete += 1
            league_summary["sport_complete"] += 1
        if b_status != 200 or bs is None:
            if b_status != 200:
                league_summary["bzz_http_errors"] += 1
            total_bzz_missing += 1
        elif not complete_both(bs):
            total_bzz_missing += 1
        else:
            total_bzz_complete += 1
            league_summary["bzz_complete"] += 1
        for provider, source_stats, source_status in (("sport", ss, s_status), ("bzz", bs, b_status)):
            if source_status == 200 and isinstance(source_stats, dict):
                for field in FIELDS:
                    if all(source_stats[side].get(field) is not None for side in ("home", "away")):
                        per_field[provider][field]["both_sides"] += 1
        if isinstance(ss, dict) and s_status == 200 and isinstance(bs, dict) and b_status == 200:
            for field in FIELDS:
                for side in ("home", "away"):
                    sv, bv = ss[side].get(field), bs[side].get(field)
                    if sv is not None and bv is not None:
                        difference = abs(sv - bv)
                        agreements[field]["n"] += 1
                        agreements[field]["exact"] += int(difference == 0)
                        agreements[field]["within1"] += int(difference <= 1)
                        agreements[field]["absdiff"] += difference
        if index % 100 == 0:
            print(f"PROGRESS compared={index}/{len(pairs)}")

    for target in sorted(by_league):
        item = by_league[target]
        print(
            "LEAGUE_RESULT " + json.dumps({
                "league": target, **item,
                "sportapi_coverage_pct": round(100 * item["sport_complete"] / item["matches"], 1) if item["matches"] else 0,
                "bzzoiro_coverage_pct": round(100 * item["bzz_complete"] / item["matches"], 1) if item["matches"] else 0,
            }, ensure_ascii=False, sort_keys=True)
        )
    for provider in ("sport", "bzz"):
        label = "sportapi" if provider == "sport" else "bzzoiro"
        for field in FIELDS:
            value = per_field[provider][field]["both_sides"]
            pct = 100 * value / processed_matches if processed_matches else 0.0
            print(f"FIELD_COVERAGE source={label} field={field} matches_both_sides={value}/{processed_matches} pct={pct:.1f}")
    for field in FIELDS:
        item = agreements[field]
        print(
            f"FIELD_AGREEMENT field={field} side_values={item['n']} exact={item['exact']} "
            f"within_1={item['within1']} mean_absolute_difference="
            f"{item['absdiff'] / item['n']:.3f}" if item["n"] else f"FIELD_AGREEMENT field={field} side_values=0"
        )
    print(
        f"TOTAL matched={processed_matches}/{len(pairs)} sportapi_complete_both={total_sport_complete} "
        f"bzzoiro_complete_both={total_bzz_complete} sportapi_incomplete_or_error={total_sport_missing} "
        f"bzzoiro_incomplete_or_error={total_bzz_missing} calls_sportapi={CALLS['sport']} "
        f"calls_bzzoiro={CALLS['bzz']} rate_limited_sportapi={RATE_LIMITED['sport']} "
        f"rate_limited_bzzoiro={RATE_LIMITED['bzz']} sportapi_requests_remaining="
        f"{min(REMAINING) if REMAINING else 'not-reported'} truncated={truncated}"
    )
    print("NOTE: cross-provider agreement measures consistency, not which value is objectively correct.")
    return 3 if RATE_LIMITED["sport"] or RATE_LIMITED["bzz"] or truncated or overflow else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
