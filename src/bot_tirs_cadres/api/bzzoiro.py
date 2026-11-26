from __future__ import annotations

from datetime import date
from typing import Any

from bot_tirs_cadres.api.client import BzzoiroHTTPClient


def records(payload: Any, *preferred_keys: str) -> list[dict[str, Any]]:
    """Convertit les formes liste ou paginées v2 en liste d'objets."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (*preferred_keys, "results", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


class BzzoiroAPI:
    def __init__(self, client: BzzoiroHTTPClient):
        self.client = client

    def coverage(self) -> dict[str, Any]:
        result = self.client.get("/coverage/", auth_required=False, ttl_seconds=300)
        return result if isinstance(result, dict) else {}

    def list_events(
        self,
        *,
        date_from: date | str,
        date_to: date | str,
        status: str,
        league_id: int | None = None,
        max_events: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "date_from": str(date_from), "date_to": str(date_to), "status": status,
            "limit": 200 if max_events is None else min(200, max(1, max_events)),
        }
        if league_id is not None:
            params["league_id"] = league_id
        offset = 0
        found: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_page_signatures = set()
        while max_events is None or len(found) < max_events:
            params["offset"] = offset
            page = self.client.get("/events/", params=params, ttl_seconds=30)
            page_records = records(page, "results")
            if not page_records:
                break
            for event in page_records:
                event_id = event.get("id")
                if event_id is None or str(event_id) not in seen_ids:
                    found.append(event)
                    if event_id is not None:
                        seen_ids.add(str(event_id))
            if not isinstance(page, dict) or not page.get("next"):
                break
            page_signature = tuple(
                str(event.get("id")) if event.get("id") is not None else repr(event)
                for event in page_records
            )
            if page_signature in seen_page_signatures:
                break
            seen_page_signatures.add(page_signature)
            offset += len(page_records)
        return found if max_events is None else found[:max_events]

    def event_stats(self, event_id: int) -> dict[str, Any]:
        result = self.client.get(f"/events/{int(event_id)}/stats/", ttl_seconds=3600)
        return result if isinstance(result, dict) else {}

    def event_incidents(self, event_id: int) -> list[dict[str, Any]]:
        result = self.client.get(f"/events/{int(event_id)}/incidents/", ttl_seconds=3600)
        return records(result, "incidents")

    def event_lineups(self, event_id: int) -> dict[str, Any]:
        result = self.client.get(f"/events/{int(event_id)}/lineups/", ttl_seconds=300)
        return result if isinstance(result, dict) else {}

    def player_profile(self, player_id: int) -> dict[str, Any]:
        result = self.client.get(f"/players/{int(player_id)}/", ttl_seconds=3600)
        return result if isinstance(result, dict) else {}

    def player_stats(
        self,
        player_id: int,
        *,
        date_from: date | str | None = None,
        date_to: date | str | None = None,
        league_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if date_from is not None:
            params["date_from"] = str(date_from)
        if date_to is not None:
            params["date_to"] = str(date_to)
        if league_id is not None:
            params["league_id"] = league_id
        result = self.client.get(f"/players/{int(player_id)}/stats/", params=params, ttl_seconds=3600)
        return records(result, "matches", "stats")

    def referee_matches(
        self,
        referee_id: int,
        *,
        date_from: date | str | None = None,
        date_to: date | str | None = None,
        league_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "finished"}
        if date_from is not None:
            params["date_from"] = str(date_from)
        if date_to is not None:
            params["date_to"] = str(date_to)
        if league_id is not None:
            params["league_id"] = league_id
        result = self.client.get(f"/referees/{int(referee_id)}/matches/", params=params, ttl_seconds=3600)
        return records(result, "matches")

    def referee_profile(self, referee_id: int) -> dict[str, Any]:
        result = self.client.get(f"/referees/{int(referee_id)}/", ttl_seconds=3600)
        return result if isinstance(result, dict) else {}
