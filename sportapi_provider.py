"""Accès GET-only à SportAPI.ai et profils historiques d'équipe."""
from __future__ import annotations
import os, time
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo
import requests

BASE_URL = "https://sportapi.ai/api"
API_KEY = os.getenv("SPORTAPI_KEY", "").strip()
PARIS = ZoneInfo("Europe/Paris")
try:
    REQUEST_LIMIT = max(0, int(os.getenv("SPORTAPI_MAX_REQUESTS", "0")))
except ValueError:
    REQUEST_LIMIT = 0
REQUEST_COUNT = 0
REQUEST_LIMIT_REACHED = False
DAILY_REMAINING = None

def sportapi_today():
    return datetime.now(PARIS).date().isoformat()

def _remaining_daily(headers):
    names = (
        "x-ratelimit-remaining-daily", "x-ratelimit-remaining-day",
        "x-rate-limit-remaining-daily", "x-rate-limit-remaining-day",
        "ratelimit-remaining-daily", "rate-limit-remaining-daily",
        "x-daily-remaining", "daily-remaining", "x-ratelimit-remaining",
        "x-rate-limit-remaining", "ratelimit-remaining", "rate-limit-remaining",
    )
    lowered = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    for name in names:
        if name in lowered:
            try:
                return max(0, int(float(lowered[name])))
            except ValueError:
                continue
    return None

def api_get(path, attempts=2):
    """Envoie uniquement des GET, avec auth dans l'en-tête et sans exposer la clé."""
    global REQUEST_COUNT, REQUEST_LIMIT_REACHED, DAILY_REMAINING
    if not API_KEY:
        return None
    for attempt in range(attempts):
        if REQUEST_LIMIT and REQUEST_COUNT >= REQUEST_LIMIT:
            REQUEST_LIMIT_REACHED = True
            return None
        if DAILY_REMAINING is not None and DAILY_REMAINING <= 0:
            REQUEST_LIMIT_REACHED = True
            return None
        try:
            REQUEST_COUNT += 1
            r = requests.get(f"{BASE_URL}/{path.lstrip('/')}",
                             headers={"X-Api-Key": API_KEY}, timeout=20)
            remaining = _remaining_daily(r.headers)
            if remaining is not None:
                DAILY_REMAINING = remaining
            elif DAILY_REMAINING is not None:
                DAILY_REMAINING = max(0, DAILY_REMAINING - 1)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, dict) and data.get("success") else None
            if r.status_code == 429 and REQUEST_LIMIT:
                REQUEST_LIMIT_REACHED = True
                break
            if r.status_code not in {429, 500, 502, 503, 504}:
                break
        except (requests.RequestException, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(1.5)
    return None

def request_usage():
    return {"requests": REQUEST_COUNT, "limit": REQUEST_LIMIT or None,
            "limit_reached": REQUEST_LIMIT_REACHED, "daily_remaining": DAILY_REMAINING}

@lru_cache(maxsize=160)
def fixtures_on_date(day):
    payload = api_get(f"fixtures/date/{day}")
    rows = payload.get("data") if payload else None
    return rows if isinstance(rows, list) else []

@lru_cache(maxsize=512)
def fixture_stats(fid):
    payload = api_get(f"fixtures/{fid}/stats")
    if not payload or payload.get("available") is False:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) and isinstance(data.get("home"), dict) and isinstance(data.get("away"), dict) else None

@lru_cache(maxsize=512)
def fixture_events(fid):
    payload = api_get(f"fixtures/{fid}/events")
    rows = payload.get("data") if payload else None
    return rows if isinstance(rows, list) else []

def _finished(fixture):
    return str(fixture.get("status", "")).upper() in {"FT", "AET", "PENS"}

@lru_cache(maxsize=512)
def _prior_fixtures(team_id, league_id, before_date, days_back, max_matches):
    end = date.fromisoformat(before_date) - timedelta(days=1)
    start = end - timedelta(days=days_back - 1)
    found = []
    for offset in range((end - start).days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        for f in fixtures_on_date(day):
            if not _finished(f):
                continue
            if league_id is not None and str(f.get("league_id")) != str(league_id):
                continue
            if str(f.get("home_id")) == str(team_id) or str(f.get("away_id")) == str(team_id):
                found.append(f)
    found.sort(key=lambda x: x.get("kickoff_utc") or x.get("datetime") or x.get("date") or "", reverse=True)
    return found[:max_matches]

def _profile(rows):
    if not rows:
        return None
    sw = sum(w for _, w in rows)
    avg = sum(v*w for v,w in rows) / sw
    var = sum(w*(v-avg)**2 for v,w in rows) / sw
    return {"average": avg, "variance": var, "samples": len(rows)}

@lru_cache(maxsize=256)
def get_team_shot_profile(team_id, preferred_side, league_id=None, before_date=None):
    """Dix matchs, même pondération domicile/extérieur que le modèle existant."""
    target = before_date or sportapi_today()
    fixtures = _prior_fixtures(str(team_id), str(league_id) if league_id is not None else None, target, 120, 10)
    observations = {"total_shots": [], "shots_on_target": []}
    for idx, f in enumerate(fixtures):
        stats = fixture_stats(f.get("id"))
        if not stats:
            continue
        side = "home" if str(f.get("home_id")) == str(team_id) else "away"
        values = stats.get(side, {})
        base = max(1.0, 1.6 - idx * 0.12)
        weight = base * (1.85 if side == preferred_side else 0.65)
        for output, field in (("total_shots", "shots"), ("shots_on_target", "shots_on_target")):
            try:
                value = float(values[field]) if values.get(field) is not None else None
            except (TypeError, ValueError):
                value = None
            if value is not None:
                observations[output].append((value, weight))
    return {key: value for key, rows in observations.items() if (value := _profile(rows))}

@lru_cache(maxsize=256)
def get_team_card_profile(team_id, preferred_side, league_id=None, before_date=None):
    """Six derniers matchs: moyennes de jaunes/fautes et meilleurs compteurs nommés.

    Fautes et compteurs par joueur sont présentés comme contexte, sans coefficient
    ajouté aux probabilités en l'absence d'estimation calibrée.
    """
    target = before_date or sportapi_today()
    fixtures = _prior_fixtures(str(team_id), str(league_id) if league_id is not None else None, target, 120, 6)
    yellow, fouls, players = [], [], {}
    for idx, f in enumerate(fixtures):
        stats = fixture_stats(f.get("id"))
        if not stats:
            continue
        side = "home" if str(f.get("home_id")) == str(team_id) else "away"
        values = stats.get(side, {})
        weight = max(1.0, 1.6 - idx * 0.12) * (1.85 if side == preferred_side else 0.65)
        for rows, key in ((yellow, "yellow_cards"), (fouls, "fouls")):
            try:
                value = float(values[key]) if values.get(key) is not None else None
            except (TypeError, ValueError):
                value = None
            if value is not None:
                rows.append((value, weight))
        for event in fixture_events(f.get("id")):
            if str(event.get("event_type", "")).lower() not in {"yellow_card", "yellow card"}:
                continue
            if str(event.get("team_side", "")).lower() != side:
                continue
            name = str(event.get("player_name") or "").strip()
            if name:
                players[name] = players.get(name, 0) + 1
    return {"yellow_cards": _profile(yellow), "fouls": _profile(fouls),
            "sample_matches": len(fixtures),
            "top_card_players": sorted(players.items(), key=lambda x: (-x[1], x[0]))[:3]}

@lru_cache(maxsize=256)
def get_team_model_form(team_id, preferred_side, league_id=None, before_date=None):
    """Variables de forme pré-match, compatibles avec le modèle walk-forward."""
    target = before_date or sportapi_today()
    fixtures = _prior_fixtures(str(team_id), str(league_id) if league_id is not None else None, target, 120, 10)
    fields = {"shots": "shots", "sot": "shots_on_target",
              "yellow": "yellow_cards", "fouls": "fouls"}
    observations = {f"{stat}_{direction}": [] for stat in fields for direction in ("for", "against")}
    for idx, fixture in enumerate(fixtures):
        stats = fixture_stats(fixture.get("id"))
        if not stats:
            continue
        side = "home" if str(fixture.get("home_id")) == str(team_id) else "away"
        other = "away" if side == "home" else "home"
        weight = max(1.0, 1.6 - idx * 0.12) * (1.85 if side == preferred_side else 0.65)
        for stat, field in fields.items():
            for direction, stat_side in (("for", side), ("against", other)):
                try:
                    value = float(stats[stat_side][field]) if stats[stat_side].get(field) is not None else None
                except (KeyError, TypeError, ValueError):
                    value = None
                if value is not None:
                    observations[f"{stat}_{direction}"].append((idx, value, weight))
    result = {}
    for name, values in observations.items():
        for window in (5, 10):
            profile = _profile([(value, weight) for idx, value, weight in values if idx < window])
            result[f"{name}_{window}"] = profile["average"] if profile else None
    return result

def reset_caches():
    fixtures_on_date.cache_clear()
    fixture_stats.cache_clear()
    fixture_events.cache_clear()
    _prior_fixtures.cache_clear()
    get_team_shot_profile.cache_clear()
    get_team_card_profile.cache_clear()
    get_team_model_form.cache_clear()
