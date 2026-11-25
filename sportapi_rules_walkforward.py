#!/usr/bin/env python3
"""Backtest walk-forward des règles SportAPI de production, sans entraînement.

Les événements et statistiques détaillés ne sont gardés qu'en mémoire. Seuls des
compteurs et scores agrégés sont écrits dans reports/.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import bot_sportapi as production
from model_rules import select_team_sot

BASE_URL = "https://sportapi.ai/api"
FINISHED = {"FT", "AET", "PENS"}
TARGET_API_NAMES = {
    "championship": "EFL Championship",
    "dutch eredivisie": "Eredivisie",
    "turkish super lig": "Süper Lig",
}
FIELDS = ("shots", "shots_on_target", "yellow_cards", "fouls")


class StopRun(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def norm(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def numeric(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def remaining_from_headers(headers, kind: str) -> int | None:
    h = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    if kind == "daily":
        names = (
            "x-ratelimit-remaining-daily", "x-ratelimit-remaining-day",
            "x-rate-limit-remaining-daily", "x-rate-limit-remaining-day",
            "ratelimit-remaining-daily", "rate-limit-remaining-daily",
            "x-daily-remaining", "daily-remaining", "x-ratelimit-remaining",
            "x-rate-limit-remaining", "ratelimit-remaining", "rate-limit-remaining",
        )
    else:
        names = (
            "x-ratelimit-remaining-monthly", "x-ratelimit-remaining-month",
            "x-rate-limit-remaining-monthly", "x-rate-limit-remaining-month",
            "ratelimit-remaining-monthly", "rate-limit-remaining-monthly",
            "x-monthly-remaining", "monthly-remaining",
        )
    for name in names:
        if name in h:
            try:
                return max(0, int(float(h[name])))
            except ValueError:
                continue
    return None


def response_rows(payload: dict) -> list:
    rows = payload.get("data", payload.get("leagues", []))
    if isinstance(rows, dict):
        rows = rows.get("data", [])
    return rows if isinstance(rows, list) else []


def make_stats(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict) or payload.get("available") is False:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("home"), dict) or not isinstance(data.get("away"), dict):
        return None
    result = {}
    for side in ("home", "away"):
        result[side] = {field: numeric(data[side].get(field)) for field in FIELDS}
    return result


class ReadOnlyClient:
    def __init__(self, api_key: str, max_requests: int):
        self.api_key = api_key
        self.max_requests = max_requests
        self.requests = 0
        self.statuses = Counter()
        self.daily_remaining: int | None = None
        self.monthly_remaining: int | None = None

    def get(self, path: str) -> dict:
        if self.requests >= self.max_requests:
            raise StopRun("request_cap", "Le plafond dur de requêtes serait dépassé.")
        if self.daily_remaining is not None and self.daily_remaining <= 0:
            raise StopRun("daily_quota_exhausted", "Le quota journalier est épuisé.")
        if self.monthly_remaining is not None and self.monthly_remaining <= 0:
            raise StopRun("monthly_quota_exhausted", "Le quota mensuel est épuisé.")
        try:
            response = requests.get(
                f"{BASE_URL}/{path.lstrip('/')}",
                headers={"X-Api-Key": self.api_key},
                timeout=25,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise StopRun("network_error", "Erreur réseau; aucune nouvelle tentative n’est effectuée.") from None
        self.requests += 1
        self.statuses[str(response.status_code)] += 1

        daily = remaining_from_headers(response.headers, "daily")
        monthly = remaining_from_headers(response.headers, "monthly")
        # Quand une réponse ne répète pas les en-têtes, décrémenter l’estimation
        # issue du dernier en-tête observé plutôt que de supposer un quota illimité.
        if daily is not None:
            self.daily_remaining = daily
        elif self.daily_remaining is not None and self.requests > 1:
            self.daily_remaining = max(0, self.daily_remaining - 1)
        if monthly is not None:
            self.monthly_remaining = monthly
        elif self.monthly_remaining is not None and self.requests > 1:
            self.monthly_remaining = max(0, self.monthly_remaining - 1)

        if response.status_code == 429:
            raise StopRun("http_429", "SportAPI a renvoyé HTTP 429; arrêt immédiat, sans retry.")
        if response.status_code != 200:
            raise StopRun("http_error", f"Réponse HTTP {response.status_code}; arrêt immédiat.")
        try:
            payload = response.json()
        except ValueError:
            raise StopRun("invalid_json", "Réponse JSON invalide; arrêt immédiat.") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise StopRun("invalid_payload", "La réponse ne confirme pas le succès de l’endpoint.")
        return payload


def profile(history: list[dict], team_id: str, league_id: str, preferred_side: str,
            target_day: date, field: str, limit: int) -> dict | None:
    lower_day = target_day - timedelta(days=120)
    candidates = [
        row for row in history
        if row["league_id"] == league_id
        and lower_day <= row["day"] < target_day
        and team_id in (row["home_id"], row["away_id"])
    ]
    candidates.sort(key=lambda row: row["sort_key"], reverse=True)
    candidates = candidates[:limit]
    observations = []
    for index, row in enumerate(candidates):
        stats = row.get("stats")
        if not stats:
            continue
        side = "home" if row["home_id"] == team_id else "away"
        value = stats[side].get(field)
        if value is None:
            continue
        base = max(1.0, 1.6 - index * 0.12)
        weight = base * (1.85 if side == preferred_side else 0.65)
        observations.append((value, weight))
    if not observations:
        return None
    total_weight = sum(weight for _, weight in observations)
    average = sum(value * weight for value, weight in observations) / total_weight
    variance = sum(weight * (value - average) ** 2 for value, weight in observations) / total_weight
    return {"average": average, "variance": variance, "samples": len(observations)}


def new_bucket() -> dict:
    return {"eligible_opportunities": 0, "signals": 0, "scored": 0,
            "unscored_outcomes": 0, "hits": 0, "probability_sum": 0.0,
            "brier_sum": 0.0}


def record(bucket: dict, predicted_probability: float, hit: bool | None) -> None:
    bucket["signals"] += 1
    if hit is None:
        bucket["unscored_outcomes"] += 1
        return
    bucket["scored"] += 1
    bucket["hits"] += int(hit)
    bucket["probability_sum"] += predicted_probability
    bucket["brier_sum"] += (predicted_probability - float(hit)) ** 2


def finalize_bucket(bucket: dict) -> dict:
    scored = bucket["scored"]
    result = {key: value for key, value in bucket.items() if key not in {"probability_sum", "brier_sum"}}
    result["hit_rate"] = round(bucket["hits"] / scored, 4) if scored else None
    result["mean_selected_probability"] = round(bucket["probability_sum"] / scored, 4) if scored else None
    result["brier_on_selected_signals"] = round(bucket["brier_sum"] / scored, 4) if scored else None
    return result


def add_coverage(coverage: dict, row: dict) -> None:
    league = row["league_label"]
    item = coverage[league]
    item["finished_fixtures"] += 1
    if row.get("stats") is not None:
        item["stats_available"] += 1
    else:
        item["stats_unavailable_or_invalid"] += 1
    for field in FIELDS:
        both = bool(row.get("stats")) and all(row["stats"][side].get(field) is not None for side in ("home", "away"))
        item["matches_with_both_team_values"][field] += int(both)


def evaluate(rows: list[dict], test_start: date, end_day: date) -> tuple[dict, dict]:
    history: list[dict] = []
    per_league = defaultdict(lambda: {
        "matches_in_test_window": 0,
        "markets": defaultdict(new_bucket),
    })
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["day"]].append(row)

    for day in sorted(by_day):
        day_rows = by_day[day]
        if test_start <= day <= end_day:
            for row in day_rows:
                league = row["league_label"]
                result = per_league[league]
                result["matches_in_test_window"] += 1
                home_id, away_id = row["home_id"], row["away_id"]
                league_id = row["league_id"]
                stats = row.get("stats")
                home_shots = profile(history, home_id, league_id, "home", day, "shots", 10)
                away_shots = profile(history, away_id, league_id, "away", day, "shots", 10)
                home_sot = profile(history, home_id, league_id, "home", day, "shots_on_target", 10)
                away_sot = profile(history, away_id, league_id, "away", day, "shots_on_target", 10)

                if home_shots and away_shots:
                    total = home_shots["average"] + away_shots["average"]
                    samples = min(home_shots["samples"], away_shots["samples"])
                    if 18 < total < 48:
                        bucket = result["markets"]["total_tirs"]
                        bucket["eligible_opportunities"] += 1
                        if home_shots["samples"] >= 1 and away_shots["samples"] >= 1:
                            over, under = production.line_probabilities(
                                total, home_shots["variance"] + away_shots["variance"], 26.5, samples
                            )
                            confidence = min(1.0, samples / 8.0)
                            over = 0.5 + (over - 0.5) * confidence
                            under = 0.5 + (under - 0.5) * confidence
                            selection, probability = max((("over", over), ("under", under)), key=lambda item: item[1])
                            odds = round(1 / probability * 1.04, 2) if probability else None
                            if samples >= 6 and probability >= 0.65 and odds >= 1.28:
                                actual_total = None
                                if stats and stats["home"].get("shots") is not None and stats["away"].get("shots") is not None:
                                    actual_total = stats["home"]["shots"] + stats["away"]["shots"]
                                actual_over = None if actual_total is None else actual_total >= 27
                                hit = None if actual_over is None else (actual_over if selection == "over" else not actual_over)
                                record(bucket, probability, hit)

                for team_id, team_profile in ((home_id, home_sot), (away_id, away_sot)):
                    if not team_profile:
                        continue
                    bucket = result["markets"]["tirs_cadres_equipe"]
                    bucket["eligible_opportunities"] += 1
                    selection = select_team_sot(team_profile, production.line_probabilities)
                    if selection:
                        line, probability = selection
                        team_side = "home" if team_id == home_id else "away"
                        actual = None
                        if stats and stats[team_side].get("shots_on_target") is not None:
                            actual = stats[team_side]["shots_on_target"] >= int(line) + 1
                        record(bucket, probability, actual)

                home_yellow = profile(history, home_id, league_id, "home", day, "yellow_cards", 6)
                away_yellow = profile(history, away_id, league_id, "away", day, "yellow_cards", 6)
                if home_yellow and away_yellow:
                    total_cards = home_yellow["average"] + away_yellow["average"]
                    samples = min(home_yellow["samples"], away_yellow["samples"])
                    if 4.5 <= total_cards < 11 and samples >= 6:
                        bucket = result["markets"]["cartons"]
                        bucket["eligible_opportunities"] += 1
                        probability, _ = production.line_probabilities(
                            total_cards, home_yellow["variance"] + away_yellow["variance"], 4.5, samples
                        )
                        if probability >= 0.65:
                            actual = None
                            if stats and stats["home"].get("yellow_cards") is not None and stats["away"].get("yellow_cards") is not None:
                                actual = stats["home"]["yellow_cards"] + stats["away"]["yellow_cards"] >= 5
                            record(bucket, probability, actual)
        # Les résultats du jour n’entrent dans les profils qu’après toutes les
        # évaluations du jour, afin d’éviter toute fuite temporelle.
        history.extend(day_rows)

    output = {}
    for league, data in per_league.items():
        output[league] = {
            "matches_in_test_window": data["matches_in_test_window"],
            "markets": {market: finalize_bucket(bucket) for market, bucket in sorted(data["markets"].items())},
        }
    return output, {"test_start": test_start.isoformat(), "test_end": end_day.isoformat(),
                    "test_days": (end_day - test_start).days + 1, "lookback_days": 120}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", required=True, help="Date de fin de l’évaluation (YYYY-MM-DD).")
    parser.add_argument("--test-days", type=int, default=27)
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--max-requests", type=int, default=700)
    parser.add_argument("--minimum-reset-quota", type=int, default=699,
                        help="Quota restant minimum après le préflight /leagues, sinon arrêt avant les fixtures.")
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args()

    end_day = date.fromisoformat(args.end_date)
    if args.test_days < 1 or args.lookback_days != 120 or args.max_requests > 700 or args.max_requests < 1:
        parser.error("La simulation exige test-days >= 1, lookback-days=120, max-requests entre 1 et 700.")
    test_start = end_day - timedelta(days=args.test_days - 1)
    collection_start = test_start - timedelta(days=args.lookback_days)
    api_key = os.getenv("SPORTAPI_KEY", "").strip()
    client = ReadOnlyClient(api_key, args.max_requests) if api_key else None

    report = {
        "report_type": "sportapi_current_rules_walk_forward",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "incomplete",
        "method": "GET only; one attempt per endpoint; no retries or redirects; no raw match-level export",
        "source": "SportAPI",
        "rules_scope": "Production selection rules; current bot code and shared team-SOT selector; no training or rule changes",
        "markets": ["total_tirs", "tirs_cadres_equipe", "cartons"],
        "period": {"collection_start": collection_start.isoformat(), "test_start": test_start.isoformat(),
                   "test_end": end_day.isoformat(), "test_days": args.test_days, "lookback_days": args.lookback_days},
        "requests": {"attempted": 0, "hard_cap": args.max_requests, "http_status_counts": {},
                     "provider_daily_remaining_last_seen": None, "provider_monthly_remaining_last_seen": None},
        "coverage_by_league": {},
        "walk_forward_by_league": {},
        "stopped_reason": None,
        "limitations": [
            "Les probabilités et taux sont conditionnels aux signaux effectivement émis par les règles; ils ne prouvent pas une rentabilité.",
            "Aucune cote historique de marché n’est collectée; pas de ROI vérifiable.",
            "Une seule période récente de 27 jours est évaluée; ce n’est pas une validation multi-saison.",
            "Les statistiques sont téléchargées en mémoire puis abandonnées; seuls les agrégats sont enregistrés.",
        ],
    }

    try:
        if not api_key:
            raise StopRun("missing_api_key", "SPORTAPI_KEY absent; aucun appel effectué.")
        if args.test_days + args.lookback_days > 150:
            raise StopRun("window_too_large", "La fenêtre demandée dépasse la limite de sécurité de 150 jours.")

        # Préflight obligatoire: l’exécution ne démarre que si les deux quotas
        # confirment qu’un run complet de taille maximale est possible.
        league_payload = client.get("leagues")
        report["requests"]["provider_daily_remaining_last_seen"] = client.daily_remaining
        report["requests"]["provider_monthly_remaining_last_seen"] = client.monthly_remaining
        if client.daily_remaining is None or client.monthly_remaining is None:
            raise StopRun("quota_headers_missing", "Les en-têtes de quota nécessaires sont absents; arrêt après le préflight.")
        if min(client.daily_remaining, client.monthly_remaining) < args.minimum_reset_quota:
            raise StopRun("quota_below_safe_minimum", "Le quota disponible après préflight est inférieur au minimum de sécurité.")

        league_rows = response_rows(league_payload)
        league_names = {
            str(row.get("id")): norm(row.get("name"))
            for row in league_rows if isinstance(row, dict) and row.get("id") is not None
        }
        target_ids = {league_id: display for league_id, name in league_names.items()
                      for target_name, display in TARGET_API_NAMES.items() if name == target_name}
        found_names = {TARGET_API_NAMES[league_names[league_id]] for league_id in target_ids}
        if found_names != set(TARGET_API_NAMES.values()):
            raise StopRun("target_leagues_not_verified", "Les trois ligues auditées ne sont pas toutes reconnues par leur nom API.")

        fixtures: dict[str, dict] = {}
        date_queries = 0
        for offset in range((end_day - collection_start).days + 1):
            day = collection_start + timedelta(days=offset)
            payload = client.get(f"fixtures/date/{day.isoformat()}")
            date_queries += 1
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise StopRun("invalid_fixture_payload", "Réponse fixtures sans liste de données.")
            for fixture in rows:
                if not isinstance(fixture, dict):
                    continue
                league_id = str(fixture.get("league_id", ""))
                if league_id not in target_ids or str(fixture.get("status", "")).upper() not in FINISHED:
                    continue
                fixture_id = fixture.get("id")
                if fixture_id is None:
                    continue
                key = str(fixture_id)
                if key in fixtures:
                    continue
                raw_day = str(fixture.get("date", ""))[:10]
                try:
                    match_day = date.fromisoformat(raw_day)
                except ValueError:
                    match_day = day
                sort_key = str(fixture.get("kickoff_utc") or fixture.get("datetime") or fixture.get("date") or raw_day)
                fixtures[key] = {
                    "fixture_id": key, "day": match_day, "sort_key": sort_key,
                    "league_id": league_id, "league_label": target_ids[league_id],
                    "home_id": str(fixture.get("home_id", "")),
                    "away_id": str(fixture.get("away_id", "")), "stats": None,
                }

        for fixture_id, row in fixtures.items():
            payload = client.get(f"fixtures/{fixture_id}/stats")
            row["stats"] = make_stats(payload)

        coverage = defaultdict(lambda: {
            "finished_fixtures": 0, "stats_available": 0,
            "stats_unavailable_or_invalid": 0,
            "matches_with_both_team_values": {field: 0 for field in FIELDS},
        })
        all_rows = list(fixtures.values())
        for row in all_rows:
            add_coverage(coverage, row)
        report["coverage_by_league"] = {league: data for league, data in sorted(coverage.items())}
        # L’absence d’un match ou d’un champ résultat ne devient jamais un hit/miss.
        walk_forward, period = evaluate(all_rows, test_start, end_day)
        report["period"].update(period)
        report["walk_forward_by_league"] = walk_forward
        report["status"] = "complete"
    except StopRun as exc:
        report["stopped_reason"] = {"code": exc.reason, "message": exc.message}

    report["requests"]["attempted"] = client.requests if client else 0
    report["requests"]["http_status_counts"] = dict(client.statuses) if client else {}
    if client:
        report["requests"]["provider_daily_remaining_last_seen"] = client.daily_remaining
        report["requests"]["provider_monthly_remaining_last_seen"] = client.monthly_remaining
        report["requests"]["date_queries_attempted"] = locals().get("date_queries", 0)
        report["requests"]["finished_fixture_stats_attempted"] = max(0, client.requests - 1 - locals().get("date_queries", 0))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "sportapi_rules_walkforward_summary.json"
    md_path = output_dir / "sportapi_rules_walkforward_summary.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = [
        "# Backtest walk-forward SportAPI — règles actuelles",
        "",
        f"- État : **{report['status']}**",
        f"- Période évaluée : {report['period']['test_start']} → {report['period']['test_end']} ({report['period']['test_days']} jours)",
        f"- Historique de chauffe : {args.lookback_days} jours avant l’évaluation",
        f"- Requêtes GET : {report['requests']['attempted']} / {args.max_requests}",
        f"- Motif d’arrêt : {report['stopped_reason']['message'] if report['stopped_reason'] else 'aucun'}",
        "",
        "## Couverture technique",
        "",
        "| Ligue | Matchs terminés collectés | Stats disponibles | Deux équipes renseignées (tirs / cadrés / jaunes / fautes) |",
        "|---|---:|---:|---|",
    ]
    for league, data in sorted(report["coverage_by_league"].items()):
        fields = data["matches_with_both_team_values"]
        coverage_text = " / ".join(str(fields[key]) for key in FIELDS)
        md.append(f"| {league} | {data['finished_fixtures']} | {data['stats_available']} | {coverage_text} |")
    md += ["", "## Résultats prédictifs conditionnels aux signaux", "",
           "Taux de réussite et Brier portent uniquement sur les sélections émises et réglables; aucun ROI n’est calculé sans cotes historiques.", "",
           "| Ligue | Marché | Occasions éligibles | Signaux | Réglés | Réussites | Taux | Proba moyenne | Brier |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for league, data in sorted(report["walk_forward_by_league"].items()):
        for market, metrics in sorted(data["markets"].items()):
            def fmt(value):
                return "—" if value is None else f"{value:.3f}"
            md.append(f"| {league} | {market} | {metrics['eligible_opportunities']} | {metrics['signals']} | {metrics['scored']} | {metrics['hits']} | {fmt(metrics['hit_rate'])} | {fmt(metrics['mean_selected_probability'])} | {fmt(metrics['brier_on_selected_signals'])} |")
    md += ["", "## Limites", ""] + [f"- {item}" for item in report["limitations"]]
    if report["stopped_reason"]:
        md += ["", f"Arrêt : {report['stopped_reason']['code']} — {report['stopped_reason']['message']}"]
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    # Console limitée aux agrégats, sans identifiants ni résultats match par match.
    print(json.dumps({
        "status": report["status"],
        "period": report["period"],
        "requests": report["requests"],
        "coverage_by_league": report["coverage_by_league"],
        "walk_forward_by_league": report["walk_forward_by_league"],
        "stopped_reason": report["stopped_reason"],
        "summary_files": [str(json_path), str(md_path)],
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
