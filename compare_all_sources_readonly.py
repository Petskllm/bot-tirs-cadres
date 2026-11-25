"""Compare des statistiques d'équipe sur les mêmes matchs, sans produire de pronostic.

GET only. Maximum 10 appels réseau au total, deux matchs terminés maximum.
Bzzoiro est utilisé uniquement pour cet audit read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

SPORT_BASE = "https://sportapi.ai/api"
FOOTBALL_BASE = "https://v3.football.api-sports.io"
BZZ_BASE = "https://sports.bzzoiro.com/api/v2"
MAX_TOTAL_GETS = 10
MAX_SAMPLE_MATCHES = 2
SPORT_RESERVE = 10
FIELDS = ("total_shots", "shots_on_target", "yellow_cards", "fouls")


class AuditStop(RuntimeError):
    pass


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def remaining_header(headers, names):
    values = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    for name in names:
        if name in values:
            try:
                return max(0, int(float(values[name])))
            except ValueError:
                pass
    return None


class ReadOnlyAudit:
    def __init__(self, sport_key, football_key, bzz_token, max_gets=MAX_TOTAL_GETS):
        self.keys = {"sportapi": sport_key, "api_football": football_key, "bzzoiro": bzz_token}
        self.max_gets = min(MAX_TOTAL_GETS, max(1, int(max_gets)))
        self.calls = defaultdict(int)
        self.errors = defaultdict(int)
        self.sport_daily_remaining = None
        self.football_daily_remaining = None

    def get(self, provider, url, headers, params=None):
        total = sum(self.calls.values())
        if total >= self.max_gets:
            raise AuditStop("plafond global de 10 GET atteint")
        if provider == "sportapi" and self.calls[provider] > 0 and self.sport_daily_remaining is None:
            raise AuditStop("arrêt SportAPI: quota journalier non vérifiable dans les en-têtes")
        if provider == "sportapi" and self.sport_daily_remaining is not None:
            if self.sport_daily_remaining <= SPORT_RESERVE:
                raise AuditStop("arrêt SportAPI pour préserver 10 GET de quota")
        if provider == "api_football" and self.football_daily_remaining is not None:
            if self.football_daily_remaining <= 10:
                raise AuditStop("arrêt API-Football pour préserver 10 GET de quota")
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20, allow_redirects=False)
        except requests.RequestException:
            self.calls[provider] += 1
            self.errors[provider] += 1
            return None
        self.calls[provider] += 1
        if provider == "sportapi":
            remaining = remaining_header(response.headers, (
                "x-ratelimit-remaining", "x-ratelimit-remaining-daily",
                "x-rate-limit-remaining", "daily-remaining",
            ))
            if remaining is not None:
                self.sport_daily_remaining = remaining
        elif provider == "api_football":
            remaining = remaining_header(response.headers, ("x-ratelimit-requests-remaining",))
            if remaining is not None:
                self.football_daily_remaining = remaining
        if response.status_code != 200:
            self.errors[provider] += 1
            return None
        try:
            payload = response.json()
        except ValueError:
            self.errors[provider] += 1
            return None
        if not isinstance(payload, dict):
            self.errors[provider] += 1
            return None
        if provider == "sportapi" and payload.get("success") is not True:
            self.errors[provider] += 1
            return None
        if provider == "api_football" and payload.get("errors"):
            self.errors[provider] += 1
            return None
        return payload

    def fixtures(self, provider, day):
        key = self.keys[provider]
        if not key:
            return None
        if provider == "sportapi":
            payload = self.get(provider, f"{SPORT_BASE}/fixtures/date/{day}", {"X-Api-Key": key})
            rows = payload.get("data", []) if payload else []
            result = []
            for row in rows if isinstance(rows, list) else []:
                result.append({
                    "key": (normalize(row.get("home_team")), normalize(row.get("away_team"))),
                    "finished": str(row.get("status", "")).upper() in {"FT", "AET", "PENS"},
                    "id": row.get("id"),
                })
            return result
        if provider == "api_football":
            payload = self.get(provider, f"{FOOTBALL_BASE}/fixtures",
                               {"x-apisports-key": key, "Accept": "application/json"}, {"date": day})
            rows = payload.get("response", []) if payload else []
            result = []
            for row in rows if isinstance(rows, list) else []:
                teams = row.get("teams", {})
                fixture = row.get("fixture", {})
                result.append({
                    "key": (normalize(teams.get("home", {}).get("name")), normalize(teams.get("away", {}).get("name"))),
                    "finished": str(fixture.get("status", {}).get("short", "")).upper() in {"FT", "AET", "PEN"},
                    "id": fixture.get("id"),
                    "home_name": teams.get("home", {}).get("name", ""),
                    "away_name": teams.get("away", {}).get("name", ""),
                })
            return result
        payload = self.get(provider, f"{BZZ_BASE}/events/",
                           {"Authorization": f"Token {key}"},
                           {"date_from": day, "date_to": day, "limit": 200})
        rows = payload.get("results", []) if payload else []
        result = []
        for row in rows if isinstance(rows, list) else []:
            result.append({
                "key": (normalize(row.get("home_team")), normalize(row.get("away_team"))),
                "finished": row.get("status") == "finished",
                "id": row.get("id"),
            })
        return result

    def stats(self, provider, fixture):
        key = self.keys[provider]
        if not key:
            return None
        if provider == "sportapi":
            payload = self.get(provider, f"{SPORT_BASE}/fixtures/{fixture['id']}/stats", {"X-Api-Key": key})
            if not payload or payload.get("available") is False:
                return None
            data = payload.get("data", {})
            return {
                side: {
                    field: number(data.get(side, {}).get("shots" if field == "total_shots" else field))
                    for field in FIELDS
                }
                for side in ("home", "away")
            }
        if provider == "api_football":
            payload = self.get(provider, f"{FOOTBALL_BASE}/fixtures/statistics",
                               {"x-apisports-key": key, "Accept": "application/json"},
                               {"fixture": fixture["id"]})
            rows = payload.get("response", []) if payload else []
            result = {}
            for row in rows if isinstance(rows, list) else []:
                team_name = row.get("team", {}).get("name", "")
                side = "home" if normalize(team_name) == normalize(fixture.get("home_name")) else (
                    "away" if normalize(team_name) == normalize(fixture.get("away_name")) else None
                )
                if not side:
                    continue
                raw = {normalize(item.get("type")): item.get("value") for item in row.get("statistics", [])}
                result[side] = {
                    "total_shots": number(raw.get("total shots")),
                    "shots_on_target": number(raw.get("shots on goal")) if raw.get("shots on goal") is not None else number(raw.get("shots on target")),
                    "yellow_cards": number(raw.get("yellow cards")),
                    "fouls": number(raw.get("fouls")),
                }
            return result or None
        payload = self.get(provider, f"{BZZ_BASE}/events/{fixture['id']}/stats/",
                           {"Authorization": f"Token {key}"})
        rows = payload.get("stats", {}) if payload else {}
        if not isinstance(rows, dict):
            return None
        result = {}
        for side in ("home", "away"):
            values = rows.get(side, {})
            result[side] = {
                "total_shots": number(first(values, "total_shots", "shots")),
                "shots_on_target": number(first(values, "shots_on_target", "shots_on_goal")),
                "yellow_cards": number(values.get("yellow_cards")),
                "fouls": number(values.get("fouls")),
            }
        return result


def number(value):
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if mapping.get(key) not in (None, "", "-"):
            return mapping[key]
    return None


def fresh_empty_summary():
    return {
        provider: {
            "enabled": False, "fixture_count": 0, "finished_fixture_count": 0,
            "stats_matches": 0, "complete_matches": 0,
            "coverage": {field: 0 for field in FIELDS},
        }
        for provider in ("sportapi", "api_football", "bzzoiro")
    }


def compare(day, max_matches=MAX_SAMPLE_MATCHES):
    audit = ReadOnlyAudit(
        os.getenv("SPORTAPI_KEY", "").strip(),
        os.getenv("API_FOOTBALL_KEY", "").strip(),
        os.getenv("BZZOIRO_TOKEN", "").strip(),
    )
    summary = fresh_empty_summary()
    provider_fixtures = {}
    provider_matches = {}
    providers = [name for name in summary if audit.keys[name]]
    for provider in providers:
        try:
            rows = audit.fixtures(provider, day) or []
        except AuditStop:
            rows = []
        provider_fixtures[provider] = rows
        summary[provider]["enabled"] = True
        summary[provider]["fixture_count"] = len(rows)
        summary[provider]["finished_fixture_count"] = sum(bool(row["finished"]) for row in rows)
        provider_matches[provider] = {row["key"]: row for row in rows if row["finished"] and all(row["key"])}

    active = [provider for provider in providers if provider in provider_matches]
    if len(active) < 2:
        common_keys = []
    else:
        common_keys = set.intersection(*(set(provider_matches[p]) for p in active))
    if len(active) >= 2:
        # Require an unambiguous team-pair match in every configured source.
        def unique_pair(rows):
            counts = defaultdict(int)
            for row in rows:
                if row["finished"] and all(row["key"]):
                    counts[row["key"]] += 1
            return {key for key, count in counts.items() if count == 1}
        unique = set.intersection(*(unique_pair(provider_fixtures[p]) for p in active))
        common_keys &= unique
    selected = sorted(common_keys)[:max(1, min(int(max_matches), MAX_SAMPLE_MATCHES))]
    differences = {field: {"pairs": 0, "exact": 0, "within_1": 0, "absolute_difference_sum": 0.0}
                   for field in FIELDS}
    pairwise = defaultdict(lambda: {field: {"pairs": 0, "exact": 0, "within_1": 0, "absolute_difference_sum": 0.0}
                                   for field in FIELDS})
    stop_reason = None
    sampled_match_count = 0
    for key in selected:
        sampled_match_count += 1
        per_match = {}
        for provider in active:
            row = provider_matches[provider][key]
            try:
                stats = audit.stats(provider, row)
            except AuditStop as exc:
                stop_reason = str(exc)
                break
            if stats is None:
                continue
            per_match[provider] = stats
            summary[provider]["stats_matches"] += 1
            complete = all(stats.get(side, {}).get(field) is not None for side in ("home", "away") for field in FIELDS)
            summary[provider]["complete_matches"] += int(complete)
            for field in FIELDS:
                if all(stats.get(side, {}).get(field) is not None for side in ("home", "away")):
                    summary[provider]["coverage"][field] += 1
        for field in FIELDS:
            for side in ("home", "away"):
                values = [(provider, per_match[provider][side].get(field))
                          for provider in per_match if per_match[provider].get(side, {}).get(field) is not None]
                for left_index in range(len(values)):
                    for right_index in range(left_index + 1, len(values)):
                        left_provider, left_value = values[left_index]
                        right_provider, right_value = values[right_index]
                        difference = abs(left_value - right_value)
                        item = "_vs_".join(sorted((left_provider, right_provider)))
                        item = pairwise[item][field]
                        item["pairs"] += 1
                        item["exact"] += int(difference == 0)
                        item["within_1"] += int(difference <= 1)
                        item["absolute_difference_sum"] += difference
        if stop_reason:
            break

    for comparisons in pairwise.values():
        for field, item in comparisons.items():
            item["mean_absolute_difference"] = round(item["absolute_difference_sum"] / item["pairs"], 3) if item["pairs"] else None
            item["exact_pct"] = round(100 * item["exact"] / item["pairs"], 1) if item["pairs"] else None
            item["within_1_pct"] = round(100 * item["within_1"] / item["pairs"], 1) if item["pairs"] else None
            del item["absolute_difference_sum"]
    return {
        "date": day,
        "mode": "GET-only; audit only; no prediction, persistence, or Telegram",
        "max_gets": MAX_TOTAL_GETS,
        "sample_limit": max(1, min(int(max_matches), MAX_SAMPLE_MATCHES)),
        "configured_sources": providers,
        "common_finished_matches": len(common_keys),
        "sampled_match_count": sampled_match_count,
        "coverage_and_requests_by_source": {
            **{key: value for key, value in summary.items()},
            "get_calls": dict(audit.calls),
            "errors": dict(audit.errors),
            "sportapi_daily_remaining_observed": audit.sport_daily_remaining,
            "api_football_daily_remaining_observed": audit.football_daily_remaining,
        },
        "pairwise_field_agreement": dict(pairwise),
        "stop_reason": stop_reason,
        "warning": "L’échantillon est descriptif et ne valide ni la qualité du fournisseur ni la performance des pronostics.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
    parser.add_argument("--max-matches", type=int, default=MAX_SAMPLE_MATCHES)
    args = parser.parse_args()
    report = compare(args.date, args.max_matches)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
