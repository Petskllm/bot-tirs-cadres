from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    api_base_url: str = "https://sports.bzzoiro.com/api/v2/"
    api_token: str | None = None
    api_token_env: str = "BZZOIRO_TOKEN"
    timeout_seconds: float = 20.0
    daily_request_limit: int = 7500
    burst_requests_per_second: int = 20
    max_retries: int = 3
    max_backoff_seconds: float = 30.0
    cache_path: Path = Path("data/bot_tirs_cadres.sqlite3")
    cache_ttl_seconds: int = 300
    future_days: int = 7
    history_days: int = 30
    max_history_matches: int = 200
    max_player_ids_per_run: int = 50
    player_ids: tuple[int, ...] = ()
    league_ids: tuple[int, ...] = ()
    last_matches: int = 10
    player_shots_on_target_thresholds: tuple[float, ...] = (0.5, 1.5)
    team_total_shots_threshold: float = 27.0
    total_cards_threshold: float = 4.5
    report_path: Path = Path("reports/football_analysis.csv")


def _tuple_of_ints(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("Les identifiants de ligues/joueurs doivent être une liste.")
    return tuple(int(item) for item in value)


def load_config(path: str | Path = "config.yaml", *, require_token: bool = True) -> AppConfig:
    """Charge les options YAML; une variable d'environnement prévaut sur le fichier."""
    config_path = Path(path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(parsed, dict):
            raise ValueError("La racine de config.yaml doit être un objet YAML.")
        raw = parsed

    api = raw.get("api", {}) or {}
    collection = raw.get("collection", {}) or {}
    analysis = raw.get("analysis", {}) or {}
    storage = raw.get("storage", {}) or {}
    report = raw.get("report", {}) or {}
    token_env = str(api.get("token_env", "BZZOIRO_TOKEN"))
    token = os.getenv(token_env)
    if require_token and not token:
        raise ValueError(f"Jeton API manquant : définis la variable {token_env}.")

    return AppConfig(
        api_base_url=str(api.get("base_url", AppConfig.api_base_url)),
        api_token=str(token) if token else None,
        api_token_env=token_env,
        timeout_seconds=float(api.get("timeout_seconds", 20)),
        daily_request_limit=int(api.get("daily_request_limit", 7500)),
        burst_requests_per_second=int(api.get("burst_requests_per_second", 20)),
        max_retries=int(api.get("max_retries", 3)),
        max_backoff_seconds=float(api.get("max_backoff_seconds", 30)),
        cache_path=Path(storage.get("sqlite_path", "data/bot_tirs_cadres.sqlite3")),
        cache_ttl_seconds=int(storage.get("cache_ttl_seconds", 300)),
        future_days=int(collection.get("future_days", 7)),
        history_days=int(collection.get("history_days", 30)),
        max_history_matches=int(collection.get("max_history_matches", 200)),
        max_player_ids_per_run=int(collection.get("max_player_ids_per_run", 50)),
        player_ids=_tuple_of_ints(collection.get("player_ids")),
        league_ids=_tuple_of_ints(collection.get("league_ids")),
        last_matches=int(analysis.get("last_matches", 10)),
        player_shots_on_target_thresholds=tuple(
            float(x) for x in analysis.get("player_shots_on_target_thresholds", [0.5, 1.5])
        ),
        team_total_shots_threshold=float(analysis.get("team_total_shots_threshold", 27)),
        total_cards_threshold=float(analysis.get("total_cards_threshold", 4.5)),
        report_path=Path(report.get("csv_path", "reports/football_analysis.csv")),
    )
