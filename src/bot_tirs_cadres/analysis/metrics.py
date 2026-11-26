from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from statistics import fmean
from typing import Any, Iterable

from bot_tirs_cadres.config import AppConfig
from bot_tirs_cadres.storage.database import SQLiteStore


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_number(data: Any, *keys: str) -> float | None:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = _number(data.get(key))
        if value is not None:
            return value
    nested = data.get("stats")
    if isinstance(nested, dict) and nested is not data:
        return _find_number(nested, *keys)
    return None


def _over_probability(values: list[float], threshold: float) -> float | None:
    if not values:
        return None
    return sum(value > threshold for value in values) / len(values)


def _row(market: str, entity_id: Any, name: str, threshold: float | str, values: list[float], note: str) -> dict[str, Any]:
    return {
        "market": market,
        "entity_id": entity_id,
        "name": name,
        "threshold": threshold,
        "observations": len(values),
        "mean": round(fmean(values), 3) if values else None,
        "estimated_probability": round(_over_probability(values, float(threshold)), 4)
        if values and isinstance(threshold, (int, float)) else None,
        "note": note,
    }


def build_report_rows(store: SQLiteStore, cfg: AppConfig) -> list[dict[str, Any]]:
    """Produit des fréquences historiques, non des probabilités calibrées ou des cotes."""
    output: list[dict[str, Any]] = []
    with store.connect() as db:
        _add_player_rows(output, db, cfg)
        _add_team_shots_rows(output, db, cfg)
        _add_team_cards_rows(output, db, cfg)
        _add_referee_rows(output, db, cfg)
    return output


def _add_player_rows(output: list[dict[str, Any]], db: sqlite3.Connection, cfg: AppConfig) -> None:
    grouped: dict[int, list[tuple[str | None, float]]] = defaultdict(list)
    names: dict[int, str] = {}
    rows = db.execute(
        "SELECT ps.player_id, ps.match_date, ps.shots_on_target, ps.raw_json, p.name "
        "FROM player_stats ps LEFT JOIN players p USING(player_id) "
        "ORDER BY ps.player_id, ps.match_date DESC"
    ).fetchall()
    for row in rows:
        names[int(row["player_id"])] = row["name"] or f"Joueur {row['player_id']}"
        value = _number(row["shots_on_target"])
        if value is None:
            raw = json.loads(row["raw_json"])
            value = _find_number(raw, "shots_on_target", "shots_on_target_total", "shotsOnTarget", "Shots (on target)")
        if value is not None:
            grouped[int(row["player_id"])].append((row["match_date"], value))
    for player_id, pairs in grouped.items():
        values = [value for _, value in pairs[: cfg.last_matches]]
        for threshold in cfg.player_shots_on_target_thresholds:
            output.append(_row(
                "joueur_tirs_cadres_plus", player_id, names[player_id], threshold, values,
                "Fréquence historique; API joueur, matchs récents disponibles.",
            ))


def _add_team_shots_rows(output: list[dict[str, Any]], db: sqlite3.Connection, cfg: AppConfig) -> None:
    grouped: dict[tuple[int, str], list[float]] = defaultdict(list)
    names: dict[int, str] = {}
    rows = db.execute(
        "SELECT m.event_id, m.home_team_id, m.home_name, m.away_team_id, m.away_name, s.raw_json "
        "FROM matches m JOIN match_stats s USING(event_id) ORDER BY m.kickoff_utc DESC"
    ).fetchall()
    for row in rows:
        payload = json.loads(row["raw_json"])
        stats = payload.get("stats", {}) if isinstance(payload, dict) else {}
        for side, team_id_key, name_key in (("home", "home_team_id", "home_name"), ("away", "away_team_id", "away_name")):
            team_id = row[team_id_key]
            if team_id is None:
                continue
            team_id = int(team_id)
            name = row[name_key] or f"Équipe {team_id}"
            names[team_id] = name
            side_stats = stats.get(side, {}) if isinstance(stats, dict) else {}
            shots = _find_number(side_stats, "total_shots", "totalShots", "shots_total")
            if shots is not None:
                grouped[(team_id, side)].append(shots)
    for (team_id, side), values in grouped.items():
        recent = values[: cfg.last_matches]
        output.append(_row(
            "equipe_tirs_totaux_plus", team_id, names.get(team_id, f"Équipe {team_id}"),
            cfg.team_total_shots_threshold, recent,
            f"Fréquence empirique des tirs > {cfg.team_total_shots_threshold:g}; échantillon domicile/extérieur {side}.",
        ))


def _add_team_cards_rows(output: list[dict[str, Any]], db: sqlite3.Connection, cfg: AppConfig) -> None:
    matches = db.execute(
        "SELECT event_id, home_team_id, home_name, away_team_id, away_name, kickoff_utc "
        "FROM matches WHERE status = 'finished' ORDER BY kickoff_utc DESC"
    ).fetchall()
    rows = db.execute(
        "SELECT event_id, incident_type, team_id, raw_json FROM incidents ORDER BY event_id"
    ).fetchall()
    match_cards: dict[tuple[int, int], int] = defaultdict(int)
    match_names: dict[int, str] = {}
    match_total: dict[int, int] = {int(match["event_id"]): 0 for match in matches}
    team_events: dict[int, list[tuple[str | None, int, int]]] = defaultdict(list)
    by_event = {int(match["event_id"]): match for match in matches}
    for row in rows:
        if row["incident_type"] != "card":
            continue
        raw = json.loads(row["raw_json"])
        if raw.get("rescinded") is True:
            continue
        match_id = int(row["event_id"])
        match = by_event.get(match_id)
        if match is None:
            continue
        team_id = row["team_id"]
        if team_id is None:
            is_home = raw.get("is_home")
            team_id = match["home_team_id"] if is_home is True else match["away_team_id"] if is_home is False else None
        if team_id is None:
            continue
        team_id = int(team_id)
        match_cards[(team_id, match_id)] += 1
        match_total[match_id] = match_total.get(match_id, 0) + 1
        name = match["home_name"] if team_id == match["home_team_id"] else match["away_name"]
        match_names[team_id] = name or f"Équipe {team_id}"
    for match in matches:
        event_id = int(match["event_id"])
        for team_id, name in ((match["home_team_id"], match["home_name"]), (match["away_team_id"], match["away_name"])):
            if team_id is not None:
                team_id = int(team_id)
                match_names[team_id] = name or match_names.get(team_id, f"Équipe {team_id}")
                team_events[team_id].append((match["kickoff_utc"], event_id, match_cards.get((team_id, event_id), 0)))
    for team_id, event_rows in team_events.items():
        values = [float(count) for _, _, count in event_rows[: cfg.last_matches]]
        output.append(_row(
            "equipe_moyenne_cartons", team_id, match_names.get(team_id, f"Équipe {team_id}"),
            "moyenne", values,
            "Cartons incidents par match; matchs sans carton inclus, côté domicile et extérieur réunis.",
        ))
    match_values = [float(match_total.get(int(match["event_id"]), 0)) for match in matches[: cfg.last_matches]]
    output.append(_row(
        "match_total_cartons_plus", "historique", "Échantillon historique", cfg.total_cards_threshold,
        match_values, "Fréquence empirique des cartons totaux > seuil; cartons annulés exclus.",
    ))


def _add_referee_rows(output: list[dict[str, Any]], db: sqlite3.Connection, cfg: AppConfig) -> None:
    profiles = db.execute(
        "SELECT referee_id, name, matches, avg_yellow_per_match, avg_red_per_match FROM referee_profiles"
    ).fetchall()
    profile_by_id = {int(row["referee_id"]): row for row in profiles}
    matches = db.execute(
        "SELECT event_id, referee_id, kickoff_utc FROM matches "
        "WHERE status = 'finished' AND referee_id IS NOT NULL ORDER BY kickoff_utc DESC"
    ).fetchall()
    card_counts: dict[int, int] = defaultdict(int)
    for incident in db.execute(
        "SELECT event_id, raw_json FROM incidents WHERE incident_type = 'card'"
    ).fetchall():
        raw = json.loads(incident["raw_json"])
        if raw.get("rescinded") is not True:
            card_counts[int(incident["event_id"])] += 1
    match_counts: dict[int, list[float]] = defaultdict(list)
    for match in matches:
        match_counts[int(match["referee_id"])].append(float(card_counts.get(int(match["event_id"]), 0)))

    for referee_id in sorted(set(profile_by_id) | set(match_counts)):
        profile = profile_by_id.get(referee_id)
        name = profile["name"] if profile and profile["name"] else f"Arbitre {referee_id}"
        recent = match_counts.get(referee_id, [])[: cfg.last_matches]
        if recent:
            output.append(_row(
                "arbitre_cartons_plus", referee_id, name, cfg.total_cards_threshold, recent,
                "Fréquence empirique issue des incidents des matchs archivés; matchs sans carton inclus.",
            ))
        if profile:
            yellow = _number(profile["avg_yellow_per_match"])
            red = _number(profile["avg_red_per_match"])
            mean = (yellow or 0.0) + (red or 0.0) if yellow is not None or red is not None else None
            output.append({
                "market": "arbitre_moyenne_cartons",
                "entity_id": referee_id,
                "name": name,
                "threshold": "moyenne carrière",
                "observations": int(profile["matches"] or 0),
                "mean": round(mean, 3) if mean is not None else None,
                "estimated_probability": None,
                "note": "Moyenne jaune + rouge du profil Bzzoiro; ce chiffre global ne donne pas une probabilité calibrée.",
            })
