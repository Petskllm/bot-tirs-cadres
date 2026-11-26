"""Audit read-only des marchés Stake disponibles sur les prochaines journées.

Authentification uniquement via STAKE_SPORTSBOOK_API_KEY; aucune mise n'est placée.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = "https://sportsbook-api.stake.com"
LANGUAGE = "en"
PAGE_SIZE = 30
PROPS_PAGE_SIZE = 100

# Noms officiels/courants vus dans le catalogue. Les ligues nationales sont
# aussi vérifiées contre leur catégorie pour exclure les homonymes d'autres pays.
NATIONAL_COMPETITIONS = {
    "premier league": {"england", "united kingdom"},
    "la liga": {"spain"},
    "serie a": {"italy"},
    "bundesliga": {"germany"},
    "ligue 1": {"france"},
}
EUROPEAN_COMPETITIONS = {
    "champions league": {"champions league", "uefa champions league"},
    "europa league": {"europa league", "uefa europa league"},
}


class StakeApiError(RuntimeError):
    """Erreur API sans divulgation de la clé ni du corps de la réponse."""


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def competition_label(tournament: dict) -> str | None:
    name = normalize_name(tournament.get("name"))
    category = normalize_name(tournament.get("category"))
    for label, categories in NATIONAL_COMPETITIONS.items():
        if name == label and category in categories:
            return label
    for label, aliases in EUROPEAN_COMPETITIONS.items():
        if name in aliases:
            return label
    return None


def parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


class StakeSportsbookClient:
    def __init__(self, api_key: str, session=None, timeout: int = 20):
        if not api_key or not api_key.strip():
            raise StakeApiError("Secret STAKE_SPORTSBOOK_API_KEY absent; aucun appel Stake effectué.")
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-Key": api_key.strip(), "Accept": "application/json"})
        self.timeout = timeout
        self.request_count = 0

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.request_count += 1
        try:
            response = self.session.get(
                f"{BASE_URL}{path}", params=params or {}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise StakeApiError(f"Erreur réseau Stake ({type(exc).__name__}) sur {path}.") from None
        if response.status_code != 200:
            # Ne pas afficher le body ni les en-têtes, qui ne sont pas nécessaires
            # au diagnostic et pourraient contenir des données sensibles.
            raise StakeApiError(f"Stake API HTTP {response.status_code} sur {path}.")
        try:
            payload = response.json()
        except ValueError:
            raise StakeApiError(f"Réponse JSON invalide sur {path}.") from None
        if not isinstance(payload, dict):
            raise StakeApiError(f"Réponse Stake inattendue sur {path}.")
        return payload

    def pages(self, path: str, params: dict | None = None) -> list[dict]:
        query = dict(params or {})
        query.setdefault("limit", PAGE_SIZE)
        query.setdefault("offset", 0)
        rows: list[dict] = []
        seen_offsets = set()
        while True:
            offset = query["offset"]
            if offset in seen_offsets:
                raise StakeApiError(f"Pagination Stake répétée sur {path}.")
            seen_offsets.add(offset)
            payload = self._get(path, query)
            data = payload.get("data")
            if not isinstance(data, list):
                raise StakeApiError(f"Liste data absente ou invalide sur {path}.")
            rows.extend(item for item in data if isinstance(item, dict))
            pagination = payload.get("pagination") or {}
            if not pagination.get("hasNextPage"):
                break
            next_offset = pagination.get("nextOffset")
            if not isinstance(next_offset, int) or next_offset <= offset:
                raise StakeApiError(f"Offset suivant invalide sur {path}.")
            query["offset"] = next_offset
        return rows

    def upcoming_market_inventory(self, days_ahead: int = 7, now: datetime | None = None) -> dict:
        if not 1 <= int(days_ahead) <= 14:
            raise ValueError("days_ahead doit être compris entre 1 et 14")
        start = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        end = start + timedelta(days=int(days_ahead))

        sports = self.pages(f"/{LANGUAGE}/sports")
        football = next(
            (item for item in sports if normalize_name(item.get("id")) == "football"), None
        )
        if football is None:
            raise StakeApiError("Le sport football est absent du catalogue Stake.")
        sport_id = str(football["id"])

        tournaments = self.pages(f"/{LANGUAGE}/sports/{sport_id}/tournaments")
        selected = []
        for tournament in tournaments:
            label = competition_label(tournament)
            if label:
                selected.append((label, tournament))

        market_rows: dict[tuple[str, str], dict] = {}
        fixtures: list[dict] = []

        def add_market(kind: str, market_name: object, fixture: dict, selections: list[dict]):
            available = [selection for selection in selections if selection.get("available")]
            if not available:
                return
            name = " ".join(str(market_name or "").split()) or "(nom absent)"
            key = (kind, name)
            row = market_rows.setdefault(key, {
                "source": kind,
                "name": name,
                "fixture_ids": set(),
                "selection_count": 0,
                "samples": [],
            })
            row["fixture_ids"].add(str(fixture["id"]))
            for selection in available:
                row["selection_count"] += 1
                if len(row["samples"]) < 5:
                    row["samples"].append({
                        "match": fixture.get("name", ""),
                        "competition": fixture.get("competition", ""),
                        "selection": selection.get("name", ""),
                        "line": selection.get("line"),
                        "decimal_odds": selection.get("decimalOdds"),
                    })

        for label, tournament in selected:
            tournament_id = str(tournament.get("id", ""))
            if not tournament_id:
                continue
            base = f"/{LANGUAGE}/sports/{sport_id}/tournaments/{tournament_id}"
            matches = self.pages(f"{base}/matches")
            for match in matches:
                kickoff = parse_datetime(match.get("startsAt"))
                if not kickoff or kickoff < start or kickoff > end:
                    continue
                if match.get("status") not in {"upcoming", "live", "suspended"}:
                    continue
                fixture = {
                    "id": str(match.get("id", "")),
                    "name": str(match.get("name", "")),
                    "competition": label,
                    "starts_at": kickoff.isoformat(),
                    "status": match.get("status"),
                    "props": bool(match.get("props")),
                }
                if not fixture["id"]:
                    continue
                fixtures.append(fixture)

                for offer in match.get("offers") or []:
                    if not isinstance(offer, dict) or offer.get("status") != "open":
                        continue
                    add_market(
                        "offer", offer.get("name"), fixture,
                        [item for item in (offer.get("selections") or []) if isinstance(item, dict)],
                    )

                # Le endpoint props apporte les marchés équipe, qui peuvent ne pas
                # figurer dans les offres principales. Aucun appel pour props=false.
                if fixture["props"]:
                    props_path = f"{base}/matches/{fixture['id']}/props"
                    props = self.pages(props_path, {"type": "team", "limit": PROPS_PAGE_SIZE})
                    for prop in props:
                        for line in prop.get("lines") or []:
                            selections = []
                            for selection in line.get("selections") or []:
                                if not isinstance(selection, dict):
                                    continue
                                selections.append({
                                    **selection,
                                    "line": line.get("line"),
                                })
                            add_market("team_prop", prop.get("market"), fixture, selections)

        markets = []
        for row in market_rows.values():
            row["fixture_count"] = len(row["fixture_ids"])
            del row["fixture_ids"]
            markets.append(row)
        markets.sort(key=lambda row: (row["source"], row["name"].casefold()))
        fixtures.sort(key=lambda row: (row["starts_at"], row["competition"], row["name"]))
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "days_ahead": int(days_ahead),
            "request_count": self.request_count,
            "competitions": sorted({label for label, _ in selected}),
            "fixtures": fixtures,
            "markets": markets,
        }


def render_markdown(inventory: dict) -> str:
    lines = [
        "## Audit des marchés Stake",
        f"Fenêtre : {inventory['window_start']} → {inventory['window_end']}",
        f"Compétitions ciblées : {', '.join(inventory['competitions']) or 'aucune trouvée'}",
        f"Matchs à venir : {len(inventory['fixtures'])} | appels API : {inventory['request_count']}",
        "",
    ]
    if not inventory["markets"]:
        lines.append("Aucune offre ouverte avec sélection disponible n’a été trouvée sur cette fenêtre.")
        return "\n".join(lines) + "\n"
    lines.extend(["### Marchés trouvés", ""])
    for market in inventory["markets"]:
        lines.append(
            f"- **{market['name']}** ({market['source']}) — "
            f"disponible sur {market['fixture_count']} match(s); "
            f"{market['selection_count']} sélection(s) disponible(s)."
        )
        for sample in market["samples"][:3]:
            odds = sample["decimal_odds"]
            odds_text = f", cote {odds}" if odds is not None else ""
            line_text = f", ligne {sample['line']}" if sample["line"] is not None else ""
            lines.append(
                f"  - {sample['competition']}: {sample['match']} — "
                f"{sample['selection']}{line_text}{odds_text}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        days_ahead = int(os.getenv("STAKE_DAYS_AHEAD", "7"))
        client = StakeSportsbookClient(os.getenv("STAKE_SPORTSBOOK_API_KEY", ""))
        inventory = client.upcoming_market_inventory(days_ahead=days_ahead)
    except (StakeApiError, ValueError) as exc:
        print(f"Échec de l’audit Stake : {exc}")
        return 1

    report_path = Path(os.getenv("STAKE_REPORT_PATH", "stake_market_inventory.json"))
    report_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(inventory)
    print(markdown)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(markdown)
    print(f"Rapport JSON écrit; appels Stake effectués : {inventory['request_count']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
