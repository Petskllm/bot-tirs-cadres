from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


class SQLiteStore:
    """Stockage local. Les réponses brutes sont conservées pour audit/retraitement."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS matches (
                    event_id INTEGER PRIMARY KEY,
                    kickoff_utc TEXT,
                    status TEXT,
                    league_id INTEGER,
                    league_name TEXT,
                    season_id INTEGER,
                    home_team_id INTEGER,
                    home_name TEXT,
                    away_team_id INTEGER,
                    away_name TEXT,
                    referee_id INTEGER,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_matches_kickoff ON matches(kickoff_utc);
                CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league_id);
                CREATE TABLE IF NOT EXISTS players (
                    player_id INTEGER PRIMARY KEY,
                    name TEXT,
                    team_id INTEGER,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS match_stats (
                    event_id INTEGER PRIMARY KEY REFERENCES matches(event_id) ON DELETE CASCADE,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_stats (
                    player_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    match_date TEXT,
                    team_id INTEGER,
                    minutes_played REAL,
                    shots_on_target REAL,
                    total_shots REAL,
                    yellow_cards REAL,
                    red_cards REAL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (player_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_player_stats_recent ON player_stats(player_id, match_date);
                CREATE TABLE IF NOT EXISTS incidents (
                    event_id INTEGER NOT NULL REFERENCES matches(event_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    incident_type TEXT,
                    player_id INTEGER,
                    team_id INTEGER,
                    minute REAL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (event_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS shots (
                    event_id INTEGER NOT NULL REFERENCES matches(event_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    player_id INTEGER,
                    shot_type TEXT,
                    xg REAL,
                    xgot REAL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (event_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS referee_matches (
                    referee_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    match_date TEXT,
                    yellow_cards REAL,
                    red_cards REAL,
                    total_cards REAL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (referee_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS referee_profiles (
                    referee_id INTEGER PRIMARY KEY,
                    name TEXT,
                    matches INTEGER,
                    avg_yellow_per_match REAL,
                    avg_red_per_match REAL,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS http_cache (
                    cache_key TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    response_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_usage (
                    utc_date TEXT PRIMARY KEY,
                    requests INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def cache_get(self, key: str) -> Any | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT expires_at, response_json FROM http_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= time.time():
                db.execute("DELETE FROM http_cache WHERE cache_key = ?", (key,))
                return None
            return json.loads(row["response_json"])

    def cache_set(self, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = time.time() + max(0, ttl_seconds)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO http_cache(cache_key, expires_at, response_json) VALUES (?, ?, ?)",
                (key, expires_at, _json(value)),
            )

    def request_count_today(self) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            row = db.execute("SELECT requests FROM request_usage WHERE utc_date = ?", (day,)).fetchone()
            return int(row["requests"]) if row else 0

    def increment_request_count(self) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO request_usage(utc_date, requests) VALUES (?, 1) "
                "ON CONFLICT(utc_date) DO UPDATE SET requests = requests + 1",
                (day,),
            )
            row = db.execute("SELECT requests FROM request_usage WHERE utc_date = ?", (day,)).fetchone()
            return int(row["requests"])

    def save_match(self, event: dict[str, Any]) -> None:
        event_id = _first(event, "id", "event_id")
        if event_id is None:
            return
        home = event.get("home_team") if isinstance(event.get("home_team"), dict) else {}
        away = event.get("away_team") if isinstance(event.get("away_team"), dict) else {}
        league = event.get("league") if isinstance(event.get("league"), dict) else {}
        referee = event.get("referee") if isinstance(event.get("referee"), dict) else {}
        kickoff = _first(event, "event_date", "start_time", "kickoff", "date", "datetime")
        with self.connect() as db:
            db.execute(
                """INSERT INTO matches
                (event_id, kickoff_utc, status, league_id, league_name, season_id,
                 home_team_id, home_name, away_team_id, away_name, referee_id, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    kickoff_utc=excluded.kickoff_utc, status=excluded.status,
                    league_id=excluded.league_id, league_name=excluded.league_name,
                    season_id=excluded.season_id, home_team_id=excluded.home_team_id,
                    home_name=excluded.home_name, away_team_id=excluded.away_team_id,
                    away_name=excluded.away_name, referee_id=excluded.referee_id,
                    raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
                (
                    int(event_id), kickoff, event.get("status"),
                    _first(event, "league_id") or league.get("id"),
                    event.get("league_name") or league.get("name"),
                    _first(event, "season_id") or (event.get("season") or {}).get("id") if isinstance(event.get("season"), dict) else event.get("season_id"),
                    _first(event, "home_team_id") or home.get("id"),
                    event.get("home_name") or event.get("home_team") or event.get("home") or home.get("name"),
                    _first(event, "away_team_id") or away.get("id"),
                    event.get("away_name") or event.get("away_team") or event.get("away") or away.get("name"),
                    _first(event, "referee_id") or referee.get("id"),
                    _json(event), self._now(),
                ),
            )

    def save_player(self, player_id: int, *, name: str | None = None, team_id: int | None = None, raw: Any = None) -> None:
        payload = raw if raw is not None else {"id": player_id, "name": name}
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO players(player_id, name, team_id, raw_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                (player_id, name, team_id, _json(payload), self._now()),
            )

    def save_match_stats(self, event_id: int, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO match_stats(event_id, raw_json, updated_at) VALUES (?, ?, ?)",
                (event_id, _json(payload), self._now()),
            )
            db.execute("DELETE FROM shots WHERE event_id = ?", (event_id,))
            for index, shot in enumerate(payload.get("shotmap") or []):
                player_id = shot.get("player_id")
                db.execute(
                    "INSERT INTO shots(event_id, sequence, player_id, shot_type, xg, xgot, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, index, player_id, shot.get("type"), shot.get("xg"), shot.get("xgot"), _json(shot)),
                )
                if player_id is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO players(player_id, name, team_id, raw_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (int(player_id), shot.get("player_name") or shot.get("name"), None, _json(shot), self._now()),
                    )

    def save_player_stat(self, player_id: int, record: dict[str, Any]) -> None:
        event_id = _first(record, "event_id", "match_id", "id")
        if event_id is None:
            # Keep logs without an event key under a stable synthetic id where possible.
            event_id = record.get("date") or record.get("match_date")
        if event_id is None:
            return
        stats = record.get("stats") if isinstance(record.get("stats"), dict) else record
        def value(*keys: str) -> Any:
            return _first(stats, *keys)
        match_date = _first(record, "event_date", "date", "match_date", "start_time")
        with self.connect() as db:
            if match_date is None:
                try:
                    row = db.execute("SELECT kickoff_utc FROM matches WHERE event_id = ?", (int(event_id),)).fetchone()
                    match_date = row["kickoff_utc"] if row else None
                except (TypeError, ValueError):
                    pass
            db.execute(
                """INSERT OR REPLACE INTO player_stats
                (player_id, event_id, match_date, team_id, minutes_played, shots_on_target,
                 total_shots, yellow_cards, red_cards, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (player_id, str(event_id), match_date,
                 _first(record, "team_id"), value("minutes_played", "minutes"),
                 value("shots_on_target", "shots_on_target_total", "shotsOnTarget", "Shots (on target)"),
                 value("total_shots", "shots", "totalShots"),
                 value("yellow_card", "yellow_cards", "yellowCards", "yellow_cards_total"),
                 value("red_card", "red_cards", "redCards", "red_cards_total"), _json(record)),
            )

    def save_incidents(self, event_id: int, incidents: Iterable[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM incidents WHERE event_id = ?", (event_id,))
            for index, item in enumerate(incidents):
                player = item.get("player") if isinstance(item.get("player"), dict) else {}
                team = item.get("team") if isinstance(item.get("team"), dict) else {}
                player_id = _first(item, "player_id") or player.get("id")
                team_id = _first(item, "team_id") or team.get("id")
                db.execute(
                    """INSERT INTO incidents(event_id, sequence, incident_type, player_id, team_id, minute, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, index, item.get("type"), player_id, team_id,
                     _first(item, "minute", "time", "period_second"), _json(item)),
                )
                if player_id is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO players(player_id, name, team_id, raw_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (int(player_id), player.get("name") or item.get("player_name"), team_id, _json(item), self._now()),
                    )

    def save_referee_matches(self, referee_id: int, records: Iterable[dict[str, Any]]) -> None:
        with self.connect() as db:
            for row in records:
                event_id = _first(row, "event_id", "id", "match_id")
                if event_id is None:
                    continue
                yellow = _first(row, "yellow_cards", "total_yellow_cards")
                red = _first(row, "red_cards", "total_red_cards")
                cards = _first(row, "total_cards")
                if cards is None and (yellow is not None or red is not None):
                    cards = float(yellow or 0) + float(red or 0)
                db.execute(
                    """INSERT OR REPLACE INTO referee_matches
                    (referee_id, event_id, match_date, yellow_cards, red_cards, total_cards, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (referee_id, event_id, _first(row, "date", "start_time", "kickoff"), yellow, red, cards, _json(row)),
                )

    def save_referee_profile(self, referee_id: int, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO referee_profiles
                (referee_id, name, matches, avg_yellow_per_match, avg_red_per_match, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (referee_id, payload.get("name"), payload.get("matches"),
                 payload.get("avg_yellow_per_match"), payload.get("avg_red_per_match"),
                 _json(payload), self._now()),
            )

    def all_player_stats(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT ps.*, p.name FROM player_stats ps LEFT JOIN players p USING(player_id) "
                "ORDER BY ps.player_id, ps.match_date DESC LIMIT ?", (limit * 1000,),
            ).fetchall()

    def all_matches_with_stats(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT m.*, s.raw_json AS stats_json FROM matches m JOIN match_stats s USING(event_id) "
                "ORDER BY m.kickoff_utc DESC"
            ).fetchall()

    def all_incidents(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT i.*, m.kickoff_utc, m.league_id, m.home_team_id, m.away_team_id, "
                "m.home_name, m.away_name, m.referee_id FROM incidents i JOIN matches m USING(event_id) "
                "ORDER BY m.kickoff_utc DESC"
            ).fetchall()
