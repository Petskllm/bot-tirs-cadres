#!/usr/bin/env python3
"""Backtest hors production du bot football.

Le script rejoue uniquement des matchs terminés et borne tous les historiques
à la date du coup d'envoi afin d'éviter la fuite de données futures.
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import requests
from model_rules import (
    BZZOIRO_TEAM_SOT_MIN_SAMPLES,
    PLAYER_SOT_MIN_AVG_MINUTES,
    PLAYER_SOT_MIN_SAMPLES,
    normalized_player_position,
    select_player_sot,
    select_team_sot,
)
from probability import line_probabilities, score_probabilities

BASE = "https://sports.bzzoiro.com/api/v2"
TOKEN = os.getenv("BZZOIRO_TOKEN")


def api_get(endpoint):
    if not TOKEN:
        return None
    try:
        response = requests.get(
            f"{BASE}/{endpoint}",
            headers={"Authorization": f"Token {TOKEN}"},
            timeout=20,
        )
        return response.json() if response.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


@lru_cache(maxsize=4096)
def cached_get(endpoint):
    return api_get(endpoint)


def parse_time(event):
    raw = str(event.get("event_date", ""))
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def date_string(value):
    return value.strftime("%Y-%m-%d")


def prior_events(team_id, kickoff, league_id=None, days=150):
    start = date_string(kickoff - timedelta(days=days))
    end = date_string(kickoff)
    league = f"&league_id={league_id}" if league_id else ""
    payload = cached_get(
        f"events/?team_id={team_id}&date_from={start}&date_to={end}"
        f"{league}&status=finished&limit=200"
    ) or {}
    events = []
    for event in payload.get("results", []):
        try:
            if event.get("status") == "finished" and parse_time(event) < kickoff:
                events.append(event)
        except (TypeError, ValueError):
            continue
    return sorted(events, key=parse_time, reverse=True)


def profile(team_id, side, kickoff, league_id, limit=10):
    fields = ("total_shots", "shots_on_target", "corner_kicks", "yellow_cards")
    values = defaultdict(list)
    events = prior_events(team_id, kickoff, league_id, days=120)[:limit]
    for index, event in enumerate(events):
        availability = cached_get(f"events/{event['id']}/availability/") or {}
        available = availability.get("available", {}) if isinstance(availability, dict) else {}
        if available.get("stats") is False:
            continue
        stats = cached_get(f"events/{event['id']}/stats/") or {}
        if not stats.get("stats"):
            continue
        event_side = "home" if event.get("home_team_id") == team_id else "away"
        data = stats["stats"].get(event_side, {})
        weight = max(1.0, 1.6 - index * 0.12) * (1.85 if event_side == side else 0.65)
        for field in fields:
            try:
                if data.get(field) is not None:
                    values[field].append((float(data[field]), weight))
            except (TypeError, ValueError):
                pass
    result = {}
    for field, observations in values.items():
        if not observations:
            continue
        total_weight = sum(weight for _, weight in observations)
        average = sum(value * weight for value, weight in observations) / total_weight
        variance = sum(weight * (value - average) ** 2 for value, weight in observations) / total_weight
        result[field] = {"average": average, "variance": variance, "samples": len(observations)}
    return result


def player_profiles(team_id, kickoff, league_id, limit=10):
    histories = defaultdict(list)
    for index, event in enumerate(prior_events(team_id, kickoff, league_id, days=90)[:limit]):
        availability = cached_get(f"events/{event['id']}/availability/") or {}
        available = availability.get("available", {}) if isinstance(availability, dict) else {}
        if available.get("player_stats") is False:
            continue
        stats = cached_get(f"events/{event['id']}/player-stats/") or {}
        weight = max(1.0, 1.5 - index * 0.1)
        for player in stats.get("player_stats", []):
            if player.get("team_id") != team_id:
                continue
            try:
                minutes = float(player.get("minutes_played", 0) or 0)
                shots_on_target = float(player.get("shots_on_target", 0) or 0)
            except (TypeError, ValueError):
                continue
            if player.get("player_id") and minutes >= 30:
                player_id = str(player["player_id"])
                position = normalized_player_position(
                    {"position": player.get("position", player.get("role", ""))}
                )
                if not position:
                    profile_data = cached_get(f"players/{player_id}/") or {}
                    if isinstance(profile_data.get("player"), dict):
                        profile_data = {**profile_data, **profile_data["player"]}
                    position = normalized_player_position(profile_data)
                histories[player_id].append((shots_on_target, weight, minutes, position))
    result = {}
    for player_id, history in histories.items():
        if len(history) < PLAYER_SOT_MIN_SAMPLES:
            continue
        weight_sum = sum(item[1] for item in history)
        average_minutes = sum(item[2] * item[1] for item in history) / weight_sum
        hit_rate = sum(item[1] for item in history if item[0] >= 1.0) / weight_sum
        positions = [item[3] for item in history if item[3]]
        # Les historiques sont ajoutés du plus récent au plus ancien.
        position = positions[0] if positions else ""
        if average_minutes < PLAYER_SOT_MIN_AVG_MINUTES:
            continue
        if not position:
            continue
        average = sum(item[0] * item[1] for item in history) / weight_sum
        variance = sum(item[1] * (item[0] - average) ** 2 for item in history) / weight_sum
        result[player_id] = {
            "average": average,
            "variance": variance,
            "samples": len(history),
            "avg_minutes": average_minutes,
            "hit_rate": hit_rate,
            "position": position,
        }
    return result


def actual_match_values(event):
    stats = cached_get(f"events/{event['id']}/stats/") or {}
    data = stats.get("stats", {})
    home = data.get("home", {})
    away = data.get("away", {})
    result = {
        "total_shots": safe_sum(home.get("total_shots"), away.get("total_shots")),
        "corners": safe_sum(home.get("corner_kicks"), away.get("corner_kicks")),
        "cards": safe_sum(home.get("yellow_cards"), away.get("yellow_cards")),
        "home_sot": number(home.get("shots_on_target")),
        "away_sot": number(away.get("shots_on_target")),
    }
    players = cached_get(f"events/{event['id']}/player-stats/") or {}
    result["player_sot"] = {
        str(player.get("player_id")): number(player.get("shots_on_target"))
        for player in players.get("player_stats", [])
        if player.get("player_id")
    }
    return result


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_sum(left, right):
    left, right = number(left), number(right)
    return left + right if left is not None and right is not None else None


def referee_card_adjustment(event):
    referee = event.get("referee")
    if isinstance(referee, dict):
        referee_id = referee.get("id")
        data = referee
    else:
        referee_id = referee
        data = {}
    if referee_id:
        remote = cached_get(f"referees/{referee_id}/") or {}
        if isinstance(remote, dict):
            data = {**data, **remote}
    try:
        average = float(data.get("avg_yellow_per_match"))
        samples = float(data.get("matches", data.get("career_games", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0
    if samples < 8 or not 0 < average < 12:
        return 0.0
    return max(-0.6, min(0.6, (average - 3.8) * 0.30))


def lineup_starter_ids(lineups):
    """Extrait les titulaires Bzzoiro : players, hors substitutes."""
    found = set()
    payload = lineups.get("lineups", lineups) if isinstance(lineups, dict) else {}
    for side in ("home", "away"):
        players = payload.get(side, {}).get("players", [])
        for player in players if isinstance(players, list) else []:
            player_id = player.get("id", player.get("player_id"))
            if player_id:
                found.add(str(player_id))
    return found


def last_official_starters(team_id, kickoff):
    """Proxy historique connu avant le coup d'envoi; la production exige la lineup du jour."""
    for event in prior_events(team_id, kickoff, days=120)[:10]:
        lineups = cached_get(f"events/{event['id']}/lineups/") or {}
        nested_payload = lineups.get("lineups") if isinstance(lineups, dict) else None
        payload = nested_payload if isinstance(nested_payload, dict) else lineups
        if not isinstance(payload, dict):
            payload = {}
        side = "home" if str(event.get("home_team_id")) == str(team_id) else "away"
        side_payload = payload.get(side)
        players = side_payload.get("players", []) if isinstance(side_payload, dict) else []
        starters = {
            str(player.get("id", player.get("player_id")))
            for player in players
            if isinstance(player, dict) and player.get("id", player.get("player_id"))
        }
        if starters:
            return starters
    return set()


def add_result(rows, market, event, selection, probability, actual, won):
    rows.append({
        "event_id": event.get("id"),
        "date": event.get("event_date"),
        "match": f"{event.get('home_team')} — {event.get('away_team')}",
        "market": market,
        "selection": selection,
        "probability": round(probability, 4),
        "actual": actual,
        "won": int(won),
    })


def run(args):
    events_payload = cached_get(
        f"events/?date_from={args.start}&date_to={args.end}&status=finished&limit=200"
    ) or {}
    events = []
    allowed = {name.lower() for name in args.leagues} if args.leagues else None
    for event in events_payload.get("results", []):
        league_name = str(event.get("league_name", event.get("league", ""))).lower()
        if allowed and league_name not in allowed:
            continue
        try:
            parse_time(event)
        except (TypeError, ValueError):
            continue
        events.append(event)
    events.sort(key=parse_time)
    events = events[:args.max_matches]

    rows = []
    skipped = defaultdict(int)
    for index, event in enumerate(events, 1):
        kickoff = parse_time(event)
        league_id = event.get("league_id")
        home_id, away_id = event.get("home_team_id"), event.get("away_team_id")
        actual = actual_match_values(event)
        # N'utilise pas la composition du match cible, connue après coup dans
        # l'API. Le backtest ne retient que les titulaires d'anciens matchs.
        starters = last_official_starters(home_id, kickoff) | last_official_starters(away_id, kickoff)
        if actual["total_shots"] is None or actual["corners"] is None or actual["cards"] is None:
            skipped["statistiques équipe incomplètes"] += 1
            continue
        home = profile(home_id, "home", kickoff, league_id)
        away = profile(away_id, "away", kickoff, league_id)
        if home.get("total_shots") and away.get("total_shots"):
            total = home["total_shots"]["average"] + away["total_shots"]["average"]
            variance = home["total_shots"]["variance"] + away["total_shots"]["variance"]
            samples = min(home["total_shots"]["samples"], away["total_shots"]["samples"])
            raw_over, raw_under = line_probabilities(total, variance, 26.5, samples)
            confidence = min(1.0, samples / 8.0)
            probabilities = [("+27", 0.5 + (raw_over - 0.5) * confidence), ("-27", 0.5 + (raw_under - 0.5) * confidence)]
            selection, probability = max(probabilities, key=lambda item: item[1])
            if 18 < total < 48 and samples >= 6 and probability >= 0.65:
                won = actual["total_shots"] >= 27 if selection == "+27" else actual["total_shots"] <= 26
                add_result(rows, "total_tirs", event, selection, probability, actual["total_shots"], won)
            else:
                skipped["total tirs sous le seuil"] += 1
        else:
            skipped["historique tirs insuffisant"] += 1

        if home.get("corner_kicks") and away.get("corner_kicks"):
            mean = home["corner_kicks"]["average"] + away["corner_kicks"]["average"]
            variance = home["corner_kicks"]["variance"] + away["corner_kicks"]["variance"]
            samples = min(home["corner_kicks"]["samples"], away["corner_kicks"]["samples"])
            probability, _ = line_probabilities(mean, variance, 9.5, samples)
            if probability >= 0.65:
                add_result(rows, "corners", event, "+9,5", probability, actual["corners"], actual["corners"] >= 10)

        if home.get("yellow_cards") and away.get("yellow_cards"):
            # La fiche arbitre actuelle peut inclure des matchs postérieurs;
            # elle est donc exclue du backtest historique.
            mean = home["yellow_cards"]["average"] + away["yellow_cards"]["average"]
            variance = home["yellow_cards"]["variance"] + away["yellow_cards"]["variance"]
            samples = min(home["yellow_cards"]["samples"], away["yellow_cards"]["samples"])
            probability, _ = line_probabilities(mean, variance, 4.5, samples)
            if 4.5 <= mean < 11 and samples >= 6 and probability >= 0.65:
                add_result(rows, "cartons", event, "+4,5", probability, actual["cards"], actual["cards"] >= 5)

        if home.get("shots_on_target") and away.get("shots_on_target"):
            for team_key, team_id, side, actual_key in (("home", home_id, "home", "home_sot"), ("away", away_id, "away", "away_sot")):
                team_profile = home if side == "home" else away
                sot = team_profile.get("shots_on_target")
                if not sot or actual[actual_key] is None:
                    continue
                selection = select_team_sot(
                    sot, line_probabilities,
                    min_samples=BZZOIRO_TEAM_SOT_MIN_SAMPLES,
                )
                if selection:
                    line, probability = selection
                    add_result(rows, "tirs_cadres_equipe", event, f"{team_key} +{line}", probability, actual[actual_key], actual[actual_key] >= line + 0.5)

        if starters:
            for team_id in (home_id, away_id):
                profiles = player_profiles(team_id, kickoff, league_id)
                for player_id, player_profile in profiles.items():
                    if player_id not in starters:
                        continue
                    selection = select_player_sot(player_profile, line_probabilities)
                    if not selection:
                        continue
                    line, probability = selection
                    label = "+0,5" if line == 0.5 else "+1,5"
                    actual_player = actual["player_sot"].get(player_id)
                    if actual_player is not None:
                        add_result(rows, "tirs_cadres_joueur", event, f"{player_id} {label}", probability, actual_player, actual_player >= line + 0.5)
        if index % 5 == 0:
            print(f"Matchs traités : {index}/{len(events)}")

    output = args.output
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["event_id", "date", "match", "market", "selection", "probability", "actual", "won"])
        writer.writeheader()
        writer.writerows(rows)

    summary = {"matches_seen": len(events), "predictions": len(rows), "skipped": dict(skipped), "markets": {}}
    for market in sorted({row["market"] for row in rows}):
        subset = [row for row in rows if row["market"] == market]
        summary["markets"][market] = score_probabilities(subset)
    summary_path = os.path.splitext(output)[0] + "_summary.json"
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Résultats détaillés : {output}")
    print(f"Résumé : {summary_path}")


def main():
    if not TOKEN:
        raise SystemExit("BZZOIRO_TOKEN est obligatoire. Aucun appel Telegram n'est utilisé.")
    parser = argparse.ArgumentParser(description="Backtest local des marchés du bot tirs cadrés")
    parser.add_argument("--start", required=True, help="Date de début YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Date de fin YYYY-MM-DD")
    parser.add_argument("--max-matches", type=int, default=30)
    parser.add_argument("--output", default="backtest_results.csv")
    parser.add_argument("--leagues", nargs="*", help="Noms exacts des ligues Bzzoiro, optionnel")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
