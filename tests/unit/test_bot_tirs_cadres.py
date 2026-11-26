from __future__ import annotations

import json
from datetime import date

import pytest

from bot_tirs_cadres.analysis.metrics import build_report_rows
from bot_tirs_cadres.api.bzzoiro import BzzoiroAPI
from bot_tirs_cadres.api.client import APIResponseError, BzzoiroHTTPClient, DailyQuotaExceeded
from bot_tirs_cadres.collectors.collector import _all_events
from bot_tirs_cadres.config import AppConfig, load_config
from bot_tirs_cadres.storage.database import SQLiteStore


class FakeResponse:
    def __init__(self, data, status_code=200, headers=None):
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}
        self.ok = status_code < 400
        self.reason = "error"
        self.text = json.dumps(data)

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_config_reads_yaml_and_env_token(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "api:\n  token_env: TEST_BZZ\ncollection:\n  future_days: 9\n  league_ids: [12, 13]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_BZZ", "secret-from-env")
    config = load_config(path)
    assert config.api_token == "secret-from-env"
    assert config.future_days == 9
    assert config.league_ids == (12, 13)


def test_config_requires_token(tmp_path, monkeypatch):
    monkeypatch.delenv("BZZOIRO_TOKEN", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("api:\n  token: this-must-not-be-used\n", encoding="utf-8")
    with pytest.raises(ValueError, match="BZZOIRO_TOKEN"):
        load_config(path)


def test_http_client_adds_token_caches_and_counts_only_network_calls(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    session = FakeSession([FakeResponse({"results": [1, 2]})])
    config = AppConfig(api_token="abc", cache_path=tmp_path / "db.sqlite3")
    client = BzzoiroHTTPClient(config, store, session=session, sleep=lambda _: None)
    first = client.get("/events/", params={"status": "upcoming"})
    second = client.get("/events/", params={"status": "upcoming"})
    assert first == second == {"results": [1, 2]}
    assert len(session.calls) == 1
    assert session.calls[0][1]["headers"]["Authorization"] == "Token abc"
    assert store.request_count_today() == 1


def test_http_client_rejects_unauthorized_response(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    session = FakeSession([FakeResponse({"detail": "Invalid token"}, status_code=401)])
    client = BzzoiroHTTPClient(AppConfig(api_token="bad"), store, session=session, sleep=lambda _: None)
    with pytest.raises(APIResponseError, match="HTTP 401"):
        client.get("/events/")


def test_http_client_daily_quota_stops_before_network(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.increment_request_count()
    session = FakeSession([])
    config = AppConfig(api_token="abc", daily_request_limit=1)
    client = BzzoiroHTTPClient(config, store, session=session)
    with pytest.raises(DailyQuotaExceeded):
        client.get("/events/")
    assert session.calls == []


def test_429_rate_limit_retries_after_header(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    session = FakeSession([
        FakeResponse({"code": "rate_limited"}, status_code=429, headers={"Retry-After": "1"}),
        FakeResponse({"ok": True}),
    ])
    waited = []
    config = AppConfig(api_token="abc", max_retries=1, max_backoff_seconds=2)
    client = BzzoiroHTTPClient(config, store, session=session, sleep=waited.append)
    assert client.get("/events/") == {"ok": True}
    assert waited == [1.0]
    assert len(session.calls) == 2


def test_store_persists_match_stats_shots_incidents_and_player_stats(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.save_match({
        "id": 101, "status": "finished", "start_time": "2026-10-01T12:00:00Z",
        "home_team_id": 1, "home": "A", "away_team_id": 2, "away": "B",
    })
    store.save_match_stats(101, {
        "stats": {"home": {"total_shots": 15}, "away": {"total_shots": 14}},
        "shotmap": [{"player_id": 500, "type": "save", "xg": 0.2, "xgot": 0.3}],
    })
    store.save_incidents(101, [{"type": "card", "minute": 30, "player_id": 500, "team_id": 1}])
    store.save_player_stat(500, {
        "event_id": 101, "date": "2026-10-01", "stats": {"shots_on_target": 2, "minutes_played": 90},
    })
    store.save_match({"id": 101, "status": "finished", "start_time": "2026-10-01T12:00:00Z"})
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        assert db.execute("SELECT shots_on_target FROM player_stats").fetchone()[0] == 2


def test_metrics_emit_player_empirical_probability(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    for event_id, shots in ((1, 2), (2, 1), (3, 0)):
        store.save_match({"id": event_id, "status": "finished", "start_time": f"2026-10-0{event_id}T12:00:00Z"})
        store.save_player_stat(10, {"event_id": event_id, "date": f"2026-10-0{event_id}", "stats": {"shots_on_target": shots}})
    rows = build_report_rows(store, AppConfig(last_matches=10, player_shots_on_target_thresholds=(0.5,)))
    player_row = next(row for row in rows if row["market"] == "joueur_tirs_cadres_plus")
    assert player_row["observations"] == 3
    assert player_row["mean"] == 1.0
    assert player_row["estimated_probability"] == pytest.approx(2 / 3, abs=0.0001)


def test_card_analysis_includes_zero_card_matches(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    for event_id in (1, 2):
        store.save_match({
            "id": event_id, "status": "finished", "start_time": f"2026-10-0{event_id}T12:00:00Z",
            "home_team_id": 10, "home": "Home FC", "away_team_id": 20, "away": "Away FC",
        })
    store.save_incidents(1, [{"type": "card", "is_home": True, "minute": 10}])
    store.save_incidents(2, [])
    rows = build_report_rows(store, AppConfig(last_matches=10, total_cards_threshold=0.5))
    team_row = next(row for row in rows if row["market"] == "equipe_moyenne_cartons" and row["entity_id"] == 10)
    total_row = next(row for row in rows if row["market"] == "match_total_cartons_plus")
    assert team_row["observations"] == 2
    assert team_row["mean"] == 0.5
    assert total_row["observations"] == 2
    assert total_row["estimated_probability"] == 0.5


def test_openapi_player_fields_and_event_date_are_normalized(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store.save_match({
        "id": 404, "status": "finished", "event_date": "2026-10-04T12:00:00Z",
        "home_team_id": 1, "home_team": "Home FC", "away_team_id": 2, "away_team": "Away FC",
        "referee_id": 77,
    })
    store.save_player_stat(900, {
        "id": 1010, "player_id": 900, "event_id": 404, "team_id": 1,
        "minutes_played": 90, "shots_on_target": 2, "total_shots": 3, "yellow_card": 1, "red_card": 0,
    })
    with store.connect() as db:
        match = db.execute("SELECT kickoff_utc, home_name, away_name FROM matches WHERE event_id = 404").fetchone()
        stat = db.execute("SELECT match_date, shots_on_target, yellow_cards, red_cards FROM player_stats").fetchone()
    assert match["kickoff_utc"] == "2026-10-04T12:00:00Z"
    assert (match["home_name"], match["away_name"]) == ("Home FC", "Away FC")
    assert stat["match_date"] == "2026-10-04T12:00:00Z"
    assert (stat["shots_on_target"], stat["yellow_cards"], stat["red_cards"]) == (2, 1, 0)


def test_referee_recent_frequency_and_profile_average(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    for event_id in (1, 2):
        store.save_match({
            "id": event_id, "status": "finished", "event_date": f"2026-10-0{event_id}T12:00:00Z",
            "referee_id": 77, "home_team_id": 1, "home_team": "Home FC",
            "away_team_id": 2, "away_team": "Away FC",
        })
    store.save_incidents(1, [{"type": "card", "is_home": True}])
    store.save_incidents(2, [])
    store.save_referee_profile(77, {
        "id": 77, "name": "Referee Example", "matches": 100,
        "avg_yellow_per_match": 4.1, "avg_red_per_match": 0.2,
    })
    rows = build_report_rows(store, AppConfig(last_matches=10, total_cards_threshold=0.5))
    recent = next(row for row in rows if row["market"] == "arbitre_cartons_plus")
    career = next(row for row in rows if row["market"] == "arbitre_moyenne_cartons")
    assert recent["estimated_probability"] == 0.5
    assert recent["observations"] == 2
    assert career["mean"] == 4.3
    assert career["observations"] == 100


def test_bzzoiro_api_paginates_beyond_the_old_default_cap():
    class PagedClient:
        def __init__(self):
            self.offsets = []

        def get(self, path, *, params, ttl_seconds):
            offset = params["offset"]
            self.offsets.append(offset)
            end = min(offset + 200, 501)
            next_page = "more" if end < 501 else None
            return {
                "results": [{"id": event_id} for event_id in range(offset, end)],
                "next": next_page,
            }

    client = PagedClient()
    api = BzzoiroAPI(client)
    events = api.list_events(
        date_from=date(2026, 11, 26), date_to=date(2026, 11, 26), status="upcoming"
    )
    assert len(events) == 501
    assert client.offsets == [0, 200, 400]


def test_upcoming_collection_is_unbounded_but_history_keeps_its_configured_limit():
    class RecordingAPI:
        def __init__(self):
            self.calls = []

        def list_events(self, **kwargs):
            self.calls.append(kwargs)
            return [{"id": 10}]

    api = RecordingAPI()
    config = AppConfig(max_history_matches=1, league_ids=(12, 13))
    upcoming = _all_events(api, date(2026, 11, 26), date(2026, 11, 27), "upcoming", config)
    assert len(upcoming) == 1
    assert all("max_events" not in call for call in api.calls)

    api.calls.clear()
    _all_events(api, date(2026, 11, 1), date(2026, 11, 26), "finished", config)
    assert [call["max_events"] for call in api.calls] == [1, 1]
