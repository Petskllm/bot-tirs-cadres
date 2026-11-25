"""Tests hors ligne du secours API-Football; aucun appel réel ni Telegram."""
import contextlib
import io
import unittest
from unittest.mock import patch

import api_football_provider as provider
import bot_sportapi as bot


class ProviderTests(unittest.TestCase):
    def test_get_uses_api_football_header_and_tracks_provider_quotas(self):
        class Response:
            status_code = 200
            headers = {
                "x-ratelimit-requests-remaining": "87",
                "X-RateLimit-Remaining": "8",
            }

            @staticmethod
            def json():
                return {"response": [], "errors": []}

        with patch.object(provider, "API_KEY", "secret-test"), \
             patch.object(provider, "REQUEST_COUNT", 0), \
             patch.object(provider, "DAILY_REMAINING", None), \
             patch.object(provider, "MINUTE_REMAINING", None), \
             patch.object(provider, "_LAST_REQUEST_AT", None), \
             patch.object(provider, "MIN_INTERVAL_SECONDS", 0), \
             patch.object(provider.requests, "get", return_value=Response()) as get:
            self.assertEqual(provider._get("fixtures", {"date": "2026-11-26"}), [])
        self.assertEqual(get.call_args.kwargs["headers"]["x-apisports-key"], "secret-test")
        self.assertEqual(get.call_args.kwargs["params"]["date"], "2026-11-26")

    def test_budget_stops_before_call_when_reserve_would_be_spent(self):
        with patch.object(provider, "API_KEY", "test"), \
             patch.object(provider, "REQUEST_COUNT", 1), \
             patch.object(provider, "DAILY_REMAINING", provider.DAILY_RESERVE), \
             patch.object(provider, "REQUEST_LIMIT_REACHED", False), \
             patch.object(provider.requests, "get") as get:
            self.assertIsNone(provider._get("fixtures", {}))
        get.assert_not_called()

    def test_only_not_started_fixtures_are_normalized(self):
        response = [
            {"fixture": {"id": 1, "date": "2026-11-26T18:00:00+00:00", "status": {"short": "NS"}},
             "league": {"id": 39, "name": "Premier League", "season": 2025},
             "teams": {"home": {"id": 10, "name": "Home"}, "away": {"id": 20, "name": "Away"}}},
            {"fixture": {"id": 2, "date": "2026-11-26T19:00:00+00:00", "status": {"short": "FT"}},
             "league": {"id": 39, "name": "Premier League", "season": 2025},
             "teams": {"home": {"id": 11, "name": "Other"}, "away": {"id": 21, "name": "Opp"}}},
        ]
        with patch.object(provider, "_get", return_value=response):
            provider.fixtures_on_date.cache_clear()
            matches = provider.fixtures_on_date("2026-11-26")
            provider.fixtures_on_date.cache_clear()
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], 1)
        self.assertEqual(matches[0]["status"], "scheduled")
        self.assertEqual(matches[0]["league_id"], 39)
        self.assertEqual(matches[0]["season"], 2025)

    def test_team_profiles_keep_separate_provider_fields_and_windows(self):
        fixture = {"fixture": {"id": 15}, "teams": {
            "home": {"id": 7, "name": "Home"}, "away": {"id": 9, "name": "Away"}}}
        sides = {"home": {"total_shots": 14, "shots_on_target": 5, "yellow_cards": 2, "fouls": 11},
                 "away": {"total_shots": 8, "shots_on_target": 3, "yellow_cards": 1, "fouls": 10}}
        with patch.object(provider, "team_history", return_value=[fixture]), \
             patch.object(provider, "_stat_sides", return_value=sides):
            result = provider.team_profiles("7", "home", "39", 2025, "2026-11-26")
        self.assertEqual(result["shot"]["total_shots"]["average"], 14)
        self.assertEqual(result["shot"]["shots_on_target"]["samples"], 1)
        self.assertEqual(result["cards"]["yellow_cards"]["average"], 2)
        self.assertEqual(result["model_form"]["shots_for_5"], 14)
        self.assertEqual(result["model_form"]["fouls_against_10"], 10)


class BotFallbackTests(unittest.TestCase):
    def test_fallback_is_opt_in_and_runs_dry_without_sportapi_or_telegram(self):
        match = {"id": 55, "status": "scheduled", "league_id": 39, "season": 2025,
                 "home_id": 11, "away_id": 12, "home_team": "Home", "away_team": "Away",
                 "league_name": "Premier League", "kickoff_utc": "2026-11-26T18:00:00Z"}
        shot = {"total_shots": {"average": 15, "variance": 2, "samples": 8}}
        cards = {"yellow_cards": {"average": 3, "variance": 1, "samples": 6},
                 "fouls": {"average": 12, "variance": 3, "samples": 6},
                 "sample_matches": 6, "top_card_players": []}
        output = io.StringIO()
        with patch.object(bot, "API_KEY", ""), patch.object(bot, "DRY_RUN", True), \
             patch.object(bot, "TRACK_ONLY", False), patch.object(bot.api_football, "API_KEY", "test-key"), \
             patch.object(bot.api_football, "today", return_value="2026-11-26"), \
             patch.object(bot.api_football, "fixtures_on_date", return_value=[match]), \
             patch.object(bot.api_football, "get_team_shot_profile", side_effect=[shot, shot]) as shots, \
             patch.object(bot.api_football, "get_team_card_profile", side_effect=[cards, cards]), \
             patch.object(bot.api_football, "get_team_model_form", return_value={}), \
             patch.object(bot.api_football, "request_usage", return_value={"requests": 10, "limit": 35,
                   "limit_reached": False, "daily_remaining": 90}), \
             patch.object(bot.api_football, "budget_exhausted", return_value=False), \
             patch.object(bot, "fixtures_on_date") as sport_fixtures, \
             patch.object(bot, "_league_map") as sport_leagues, \
             patch.object(bot, "record_predictions") as record, patch.object(bot, "build_reports") as reports, \
             patch.object(bot, "_send_telegram") as send, \
             patch.dict("os.environ", {"API_FOOTBALL_FALLBACK": "true"}, clear=False), \
             contextlib.redirect_stdout(output):
            bot.main()
        self.assertEqual(shots.call_count, 2)
        sport_fixtures.assert_not_called()
        sport_leagues.assert_not_called()
        record.assert_not_called()
        reports.assert_not_called()
        send.assert_not_called()
        self.assertIn("PRONOSTICS API-FOOTBALL — SECOURS", output.getvalue())
        self.assertIn("aucun message Telegram envoyé", output.getvalue())


if __name__ == "__main__":
    unittest.main()
