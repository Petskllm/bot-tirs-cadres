from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from bot_tirs_cadres.api.bzzoiro import BzzoiroAPI
from bot_tirs_cadres.api.client import APIResponseError
from bot_tirs_cadres.config import AppConfig
from bot_tirs_cadres.storage.database import SQLiteStore

logger = logging.getLogger(__name__)


def _id(obj: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, dict):
            value = value.get("id")
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _all_events(api: BzzoiroAPI, start: date, end: date, status: str, cfg: AppConfig) -> list[dict[str, Any]]:
    league_ids: tuple[int | None, ...] = cfg.league_ids or (None,)
    combined: dict[int, dict[str, Any]] = {}
    each_limit = max(1, cfg.max_history_matches)
    for league_id in league_ids:
        events = api.list_events(
            date_from=start, date_to=end, status=status, league_id=league_id,
            max_events=each_limit,
        )
        for event in events:
            event_id = _id(event, "id", "event_id")
            if event_id is not None:
                combined[event_id] = event
    return list(combined.values())


def collect_data(api: BzzoiroAPI, store: SQLiteStore, cfg: AppConfig, *, today: date | None = None) -> dict[str, int]:
    """Collecte une fenêtre de matchs; les réponses déjà en cache évitent les appels répétés."""
    current_day = today or datetime.now(timezone.utc).date()
    future = _all_events(
        api, current_day, current_day + timedelta(days=max(0, cfg.future_days)), "upcoming", cfg
    )
    historical = _all_events(
        api, current_day - timedelta(days=max(1, cfg.history_days)), current_day,
        "finished", cfg,
    )
    historical.sort(
        key=lambda item: str(item.get("event_date") or item.get("start_time") or item.get("date") or ""),
        reverse=True,
    )
    historical = historical[: max(0, cfg.max_history_matches)]

    for event in (*historical, *future):
        store.save_match(event)

    discovered_player_ids = set(cfg.player_ids)
    referee_ids: set[int] = set()
    for event in historical:
        event_id = _id(event, "id", "event_id")
        if event_id is None:
            continue
        referee_id = _id(event, "referee_id", "referee")
        if referee_id:
            referee_ids.add(referee_id)
        try:
            stats = api.event_stats(event_id)
            store.save_match_stats(event_id, stats)
            for shot in stats.get("shotmap") or []:
                player_id = _id(shot, "player_id", "player")
                if player_id:
                    discovered_player_ids.add(player_id)
        except APIResponseError as exc:
            if exc.status not in (404, 503):
                raise
            logger.warning("Stats indisponibles pour le match %s: %s", event_id, exc)
        try:
            incidents = api.event_incidents(event_id)
            store.save_incidents(event_id, incidents)
            for incident in incidents:
                player_id = _id(incident, "player_id", "player")
                if player_id:
                    discovered_player_ids.add(player_id)
        except APIResponseError as exc:
            if exc.status not in (404, 503):
                raise
            logger.warning("Incidents indisponibles pour le match %s: %s", event_id, exc)

    max_players = max(0, cfg.max_player_ids_per_run)
    priority_players = list(dict.fromkeys(cfg.player_ids))
    remaining_players = sorted(discovered_player_ids - set(priority_players))
    selected_players = (priority_players + remaining_players)[:max_players]
    start = current_day - timedelta(days=max(1, cfg.history_days))
    for player_id in selected_players:
        try:
            profile = api.player_profile(player_id)
            if profile:
                store.save_player(
                    player_id, name=profile.get("name"), team_id=_id(profile, "current_team_id"), raw=profile
                )
        except APIResponseError as exc:
            if exc.status not in (404, 503):
                raise
            logger.warning("Profil joueur indisponible pour %s: %s", player_id, exc)
        try:
            rows = api.player_stats(player_id, date_from=start, date_to=current_day)
            for row in rows:
                store.save_player_stat(player_id, row)
                name = row.get("player_name") or row.get("name")
                if name:
                    store.save_player(player_id, name=name, team_id=_id(row, "team_id"), raw=row)
        except APIResponseError as exc:
            if exc.status not in (404, 503):
                raise
            logger.warning("Stats joueur indisponibles pour %s: %s", player_id, exc)

    # Le profil arbitre fournit ses moyennes de cartons; les incidents gardent le détail récent.
    for referee_id in sorted(referee_ids)[:50]:
        try:
            profile = api.referee_profile(referee_id)
            if profile:
                store.save_referee_profile(referee_id, profile)
        except APIResponseError as exc:
            if exc.status not in (404, 503):
                raise
            logger.warning("Profil arbitre indisponible pour %s: %s", referee_id, exc)

    return {
        "upcoming_matches": len(future),
        "historical_matches": len(historical),
        "player_ids_requested": len(selected_players),
        "referees_requested": min(len(referee_ids), 50),
    }
