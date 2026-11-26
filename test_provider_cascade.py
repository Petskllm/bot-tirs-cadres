"""Tests hors ligne de la cascade multi-fournisseurs; aucun appel réseau réel."""
import contextlib
import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_diagnostics import retry_after_seconds, summarize_http_error
from workflow_outputs import set_github_output


class ProviderDiagnosticsTests(unittest.TestCase):
    def test_retry_after_supports_seconds_dates_and_invalid_values(self):
        self.assertEqual(retry_after_seconds({"Retry-After": "12"}), 12.0)
        self.assertIsNone(retry_after_seconds({"Retry-After": "not a date"}))
        self.assertIsNone(retry_after_seconds({}))

    def test_http_summary_exposes_category_without_echoing_payload_values(self):
        secret = "private-error-value"
        result = summarize_http_error(
            "SportAPI", 429, {"Retry-After": "5"},
            payload={"errors": {"message": secret, "code": "quota"}},
        )
        self.assertIn("HTTP 429", result)
        self.assertIn("quota/rate-limit", result)
        self.assertIn("champs=code,message", result)
        self.assertIn("Retry-After=5s", result)
        self.assertNotIn(secret, result)

    def test_github_output_is_single_line_and_safe_outside_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                self.assertTrue(set_github_output("has_predictions", True))
                with self.assertRaises(ValueError):
                    set_github_output("has_predictions", "true\nother=value")
            self.assertEqual(output.read_text(encoding="utf-8"), "has_predictions=true\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(set_github_output("has_predictions", False))

    def test_sportapi_429_is_recorded_without_retrying_or_echoing_body(self):
        provider = importlib.import_module("sportapi_provider")
        class Response:
            status_code = 429
            headers = {"Retry-After": "8"}
            text = "private-body-value"
            @staticmethod
            def json():
                return {"errors": {"message": "private-body-value"}}
        provider.reset_collection_errors()
        with patch.object(provider, "API_KEY", "private-api-key"), \
             patch.object(provider, "REQUEST_COUNT", 0), \
             patch.object(provider, "REQUEST_LIMIT", 100), \
             patch.object(provider, "REQUEST_LIMIT_REACHED", False), \
             patch.object(provider, "DAILY_REMAINING", None), \
             patch.object(provider.requests, "get", return_value=Response()) as get, \
             patch.object(provider.time, "sleep"):
            self.assertIsNone(provider.api_get("fixtures/date/2026-11-26"))
            self.assertTrue(provider.REQUEST_LIMIT_REACHED)
            self.assertEqual(get.call_count, 1)
        self.assertTrue(provider.COLLECTION_ERRORS)
        self.assertNotIn("private-body-value", repr(provider.COLLECTION_ERRORS))

    def test_sportapi_daily_quota_zero_on_response_marks_request_budget_exhausted(self):
        provider = importlib.import_module("sportapi_provider")
        class Response:
            status_code = 200
            headers = {"x-ratelimit-remaining-daily": "0"}
            @staticmethod
            def json():
                return {"success": True, "data": []}
        with patch.object(provider, "API_KEY", "private-api-key"), \
             patch.object(provider, "REQUEST_COUNT", 0), \
             patch.object(provider, "REQUEST_LIMIT", 100), \
             patch.object(provider, "REQUEST_LIMIT_REACHED", False), \
             patch.object(provider, "DAILY_REMAINING", None), \
             patch.object(provider.requests, "get", return_value=Response()):
            self.assertEqual(provider.api_get("leagues"), {"success": True, "data": []})
            self.assertTrue(provider.REQUEST_LIMIT_REACHED)


class CascadeHandoffTests(unittest.TestCase):
    @staticmethod
    def bzz_module():
        with patch.dict(os.environ, {"BZZOIRO_TOKEN": "offline-test", "DRY_RUN": "1"}, clear=False):
            return importlib.import_module("test_bzzoiro")

    def test_partial_bzzoiro_response_is_not_published_and_marks_source_unusable(self):
        bot = self.bzz_module()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "out"
            with patch.object(bot, "SOURCE_FALLBACK_ENABLED", True), \
                 patch.object(bot, "TOKEN", "offline-test"), \
                 patch.object(bot, "DRY_RUN", True), patch.object(bot, "TRACK_ONLY", False), \
                 patch.object(bot, "should_persist_results", return_value=False), \
                 patch.object(bot, "api_get", return_value={"results": []}), \
                 patch.object(bot, "get_daily_events", return_value=({"results": [{"id": 1}]}, "HTTP 429")), \
                 patch.object(bot, "validate_matches") as validate, \
                 patch.object(bot, "send_telegram") as send, \
                 patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}), \
                 contextlib.redirect_stdout(output):
                bot.main()
            validate.assert_not_called()
            send.assert_not_called()
            self.assertEqual(output_file.read_text(encoding="utf-8"), "has_predictions=false\n")
            self.assertIn("Résultat Bzzoiro incomplet", output.getvalue())

    def test_sportapi_failure_is_not_reported_as_a_confirmed_empty_match_day(self):
        bot = importlib.import_module("bot_sportapi")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "out"
            with patch.object(bot, "SOURCE_FALLBACK_ENABLED", True), \
                 patch.object(bot, "API_KEY", "offline-test"), \
                 patch.object(bot, "DRY_RUN", True), patch.object(bot, "TRACK_ONLY", False), \
                 patch.object(bot, "sportapi_today", return_value="2026-11-26"), \
                 patch.object(bot, "fixtures_on_date", return_value=[]), \
                 patch.object(bot.sportapi, "reset_collection_errors"), \
                 patch.object(bot.sportapi, "LAST_ERROR", "SportAPI HTTP 503; catégorie=erreur serveur"), \
                 patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file), "API_FOOTBALL_FALLBACK": "false"}), \
                 contextlib.redirect_stdout(output):
                bot.main()
            self.assertIn("indisponible", output.getvalue())
            self.assertNotIn("a répondu sans fixture", output.getvalue())
            self.assertEqual(output_file.read_text(encoding="utf-8"), "has_predictions=false\n")

    def test_sportapi_stat_collection_error_discards_candidate_selections(self):
        bot = importlib.import_module("bot_sportapi")
        match = {"id": 12, "status": "scheduled", "league_id": 1,
                 "home_id": 21, "away_id": 22, "home_team": "Home", "away_team": "Away"}
        shot = {"total_shots": {"average": 15, "variance": 2, "samples": 10},
                "shots_on_target": {"average": 5, "variance": 1, "samples": 10}}
        usage = {"requests": 5, "limit": 100, "limit_reached": False, "daily_remaining": 200}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "out"
            with patch.object(bot, "SOURCE_FALLBACK_ENABLED", True), \
                 patch.object(bot, "API_KEY", "offline-test"), \
                 patch.object(bot, "DRY_RUN", True), patch.object(bot, "TRACK_ONLY", False), \
                 patch.object(bot, "sportapi_today", return_value="2026-11-26"), \
                 patch.object(bot, "fixtures_on_date", return_value=[match]), \
                 patch.object(bot, "_league_map", return_value={1: "premier league"}), \
                 patch.object(bot, "load_models", return_value={}), \
                 patch.object(bot, "get_team_shot_profile", side_effect=[shot, shot]), \
                 patch.object(bot, "get_team_card_profile", return_value={}), \
                 patch.object(bot, "line_probabilities", return_value=(0.8, 0.2)), \
                 patch.object(bot, "request_usage", return_value=usage), \
                 patch.object(bot.sportapi, "reset_collection_errors"), \
                 patch.object(bot.sportapi, "COLLECTION_ERRORS", ["SportAPI HTTP 503; catégorie=erreur serveur"]), \
                 patch.object(bot.sportapi, "DAILY_REMAINING", 200), \
                 patch.object(bot, "record_predictions") as record, \
                 patch.object(bot, "_send_telegram") as send, \
                 patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file), "API_FOOTBALL_FALLBACK": "false"}), \
                 contextlib.redirect_stdout(output):
                bot.main()
            record.assert_not_called()
            send.assert_not_called()
            self.assertIn("erreur(s) de collecte", output.getvalue())
            self.assertEqual(output_file.read_text(encoding="utf-8"), "has_predictions=false\n")


if __name__ == "__main__":
    unittest.main()
