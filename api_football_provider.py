"""Fournisseur de secours API-Football, GET-only et séparé de SportAPI."""
from __future__ import annotations

import os
import time
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import requests

from api_only_stats import extract
from provider_diagnostics import summarize_http_error

BASE_URL = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
PARIS = ZoneInfo("Europe/Paris")
REQUEST_LIMIT = max(1, min(100, int(os.getenv("API_FOOTBALL_MAX_REQUESTS", "35"))))
DAILY_RESERVE = 15
MIN_INTERVAL_SECONDS = 6.2
REQUEST_COUNT = 0
REQUEST_LIMIT_REACHED = False
DAILY_REMAINING = None
MINUTE_REMAINING = None
_LAST_REQUEST_AT = None


def today():
    return datetime.now(PARIS).date().isoformat()


def _headers_remaining(headers, names):
    lowered = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    for name in names:
        if name.lower() in lowered:
            try:
                return max(0, int(float(lowered[name.lower()])))
            except ValueError:
                pass
    return None


def _get(path, params=None):
    """One authenticated GET; stop on missing key, quota cap or provider errors."""
    global REQUEST_COUNT, REQUEST_LIMIT_REACHED, DAILY_REMAINING, MINUTE_REMAINING, _LAST_REQUEST_AT
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY absente")
    if REQUEST_COUNT >= REQUEST_LIMIT or (DAILY_REMAINING is not None and DAILY_REMAINING <= DAILY_RESERVE):
        REQUEST_LIMIT_REACHED = True
        return None
    if _LAST_REQUEST_AT is not None:
        time.sleep(max(0.0, MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)))
    REQUEST_COUNT += 1
    _LAST_REQUEST_AT = time.monotonic()
    try:
        response = requests.get(
            f"{BASE_URL}/{path.lstrip('/')}",
            params=params or {},
            headers={"x-apisports-key": API_KEY, "Accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException:
        raise RuntimeError("Erreur réseau API-Football") from None
    DAILY_REMAINING = _headers_remaining(response.headers, ("x-ratelimit-requests-remaining",))
    MINUTE_REMAINING = _headers_remaining(response.headers, ("x-ratelimit-remaining",))
    if DAILY_REMAINING is not None and DAILY_REMAINING <= DAILY_RESERVE:
        REQUEST_LIMIT_REACHED = True
    if response.status_code == 429:
        REQUEST_LIMIT_REACHED = True
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = None
        raise RuntimeError(summarize_http_error(
            "API-Football", response.status_code, response.headers,
            payload=error_payload, text=getattr(response, "text", ""),
        ))
    if response.status_code != 200:
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = None
        raise RuntimeError(summarize_http_error(
            "API-Football", response.status_code, response.headers,
            payload=error_payload, text=getattr(response, "text", ""),
        ))
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("Réponse JSON invalide API-Football") from None
    if not isinstance(payload, dict) or payload.get("errors"):
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, dict) and any("limit" in str(key).lower() for key in errors):
            REQUEST_LIMIT_REACHED = True
        raise RuntimeError(summarize_http_error(
            "API-Football", response.status_code, response.headers, payload=payload,
        ))
    return payload.get("response", [])


def request_usage():
    return {"requests": REQUEST_COUNT, "limit": REQUEST_LIMIT,
            "limit_reached": REQUEST_LIMIT_REACHED, "daily_remaining": DAILY_REMAINING,
            "minute_remaining": MINUTE_REMAINING}


def budget_exhausted():
    return REQUEST_LIMIT_REACHED or REQUEST_COUNT >= REQUEST_LIMIT


def reset_state():
    global REQUEST_COUNT, REQUEST_LIMIT_REACHED, DAILY_REMAINING, MINUTE_REMAINING, _LAST_REQUEST_AT
    REQUEST_COUNT = 0
    REQUEST_LIMIT_REACHED = False
    DAILY_REMAINING = None
    MINUTE_REMAINING = None
    _LAST_REQUEST_AT = None
    fixtures_on_date.cache_clear()
    team_history.cache_clear()
    fixture_statistics.cache_clear()
    team_profiles.cache_clear()


@lru_cache(maxsize=60)
def fixtures_on_date(day):
    rows = _get("fixtures", {"date": day, "timezone": "Europe/Paris"})
    result = []
    for item in rows or []:
        fixture = item.get("fixture", {})
        league = item.get("league", {})
        teams = item.get("teams", {})
        home, away = teams.get("home", {}), teams.get("away", {})
        status = fixture.get("status", {}).get("short")
        if status != "NS":
            continue
        result.append({
            "id": fixture.get("id"), "status": "scheduled",
            "league_id": league.get("id"), "league_name": league.get("name", ""),
            "season": league.get("season"), "home_id": home.get("id"),
            "away_id": away.get("id"), "home_team": home.get("name", "Domicile"),
            "away_team": away.get("name", "Extérieur"), "kickoff_utc": fixture.get("date"),
        })
    return result


@lru_cache(maxsize=256)
def team_history(team_id, league_id, season, before_date, count=10):
    if not all((team_id, league_id, season)):
        return []
    rows = _get("fixtures", {"team": team_id, "league": league_id, "season": season, "last": count})
    finished = []
    for item in rows or []:
        fixture = item.get("fixture", {})
        status = fixture.get("status", {}).get("short")
        kickoff = fixture.get("date", "")
        if status not in {"FT", "AET", "PEN"} or not kickoff or kickoff[:10] >= before_date:
            continue
        finished.append(item)
    finished.sort(key=lambda item: item.get("fixture", {}).get("date", ""), reverse=True)
    return finished[:count]


@lru_cache(maxsize=512)
def fixture_statistics(fixture_id):
    rows = _get("fixtures/statistics", {"fixture": fixture_id})
    return rows if isinstance(rows, list) else []


def _stat_sides(item):
    fixture = item.get("fixture", {})
    fixture_id = fixture.get("id")
    if fixture_id is None:
        return None
    teams = item.get("teams", {})
    home_name = teams.get("home", {}).get("name", "")
    away_name = teams.get("away", {}).get("name", "")
    stats = fixture_statistics(fixture_id)
    home, away = extract(stats, home_name), extract(stats, away_name)
    return {"home": home, "away": away}


def _profile(rows):
    if not rows:
        return None
    weight_sum = sum(weight for _, weight in rows)
    avg = sum(value * weight for value, weight in rows) / weight_sum
    variance = sum(weight * (value - avg) ** 2 for value, weight in rows) / weight_sum
    return {"average": avg, "variance": variance, "samples": len(rows)}


@lru_cache(maxsize=256)
def team_profiles(team_id, preferred_side, league_id, season, before_date):
    fixtures = team_history(str(team_id), str(league_id), int(season), before_date, 10)
    shots = {"total_shots": [], "shots_on_target": []}
    yellows, fouls = [], []
    forms = {f"{field}_{direction}": [] for field in ("shots", "sot", "yellow", "fouls")
             for direction in ("for", "against")}
    for index, item in enumerate(fixtures):
        sides = _stat_sides(item)
        if not sides:
            continue
        teams = item.get("teams", {})
        side = "home" if str(teams.get("home", {}).get("id")) == str(team_id) else "away"
        other = "away" if side == "home" else "home"
        weight = max(1.0, 1.6 - index * 0.12) * (1.85 if side == preferred_side else 0.65)
        values = sides[side]
        for output, field in (("total_shots", "total_shots"), ("shots_on_target", "shots_on_target")):
            raw = values.get(field)
            if isinstance(raw, (int, float)):
                shots[output].append((float(raw), weight))
        if index < 6:
            for bucket, field in ((yellows, "yellow_cards"), (fouls, "fouls")):
                raw = values.get(field)
                if isinstance(raw, (int, float)):
                    bucket.append((float(raw), weight))
        stat_fields = {"shots": "total_shots", "sot": "shots_on_target",
                       "yellow": "yellow_cards", "fouls": "fouls"}
        for key, field in stat_fields.items():
            for direction, source_side in (("for", side), ("against", other)):
                raw = sides[source_side].get(field)
                if isinstance(raw, (int, float)):
                    forms[f"{key}_{direction}"].append((index, float(raw), weight))
    model_form = {}
    for name, values in forms.items():
        for window in (5, 10):
            selected = [(value, weight) for index, value, weight in values if index < window]
            profile = _profile(selected)
            model_form[f"{name}_{window}"] = profile["average"] if profile else None
    return {
        "shot": {key: value for key, values in shots.items() if (value := _profile(values))},
        "cards": {"yellow_cards": _profile(yellows), "fouls": _profile(fouls),
                  "sample_matches": len(fixtures), "top_card_players": []},
        "model_form": model_form,
    }


def get_team_shot_profile(team_id, preferred_side, league_id=None, before_date=None, season=None):
    if season is None:
        return {}
    return team_profiles(str(team_id), preferred_side, str(league_id), int(season), before_date or today())["shot"]


def get_team_card_profile(team_id, preferred_side, league_id=None, before_date=None, season=None):
    if season is None:
        return {}
    return team_profiles(str(team_id), preferred_side, str(league_id), int(season), before_date or today())["cards"]


def get_team_model_form(team_id, preferred_side, league_id=None, before_date=None, season=None):
    if season is None:
        return {}
    return team_profiles(str(team_id), preferred_side, str(league_id), int(season), before_date or today())["model_form"]


@lru_cache(maxsize=256)
def actuals_for_fixture(fixture_id):
    fixtures = _get("fixtures", {"id": fixture_id})
    if not fixtures:
        return {}
    teams = fixtures[0].get("teams", {})
    home_name = teams.get("home", {}).get("name", "")
    away_name = teams.get("away", {}).get("name", "")
    if not home_name or not away_name:
        return {}
    rows = fixture_statistics(fixture_id)
    if len(rows) < 2:
        return {}
    home, away = extract(rows, home_name), extract(rows, away_name)
    actuals = {}
    for key in ("total_shots", "yellow_cards"):
        first, second = home.get(key), away.get(key)
        if isinstance(first, (int, float)) and isinstance(second, (int, float)):
            actuals["total_tirs" if key == "total_shots" else "cartons"] = float(first + second)
    actuals["home_sot"] = float(home["shots_on_target"]) if isinstance(home.get("shots_on_target"), (int, float)) else None
    actuals["away_sot"] = float(away["shots_on_target"]) if isinstance(away.get("shots_on_target"), (int, float)) else None
    return actuals
