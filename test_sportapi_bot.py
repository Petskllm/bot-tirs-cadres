import contextlib
import io
import unittest
from unittest.mock import patch

import bot_sportapi as bot


class SportAPIBotModeTests(unittest.TestCase):
    @staticmethod
    def live_match():
        return {"id": 75, "status": "Scheduled", "league_id": 1,
                "home_id": 21, "away_id": 22, "home_team": "Home", "away_team": "Away"}

    def test_dry_run_only_reports_three_supported_markets_without_side_effects(self):
        match = {"id": 55, "status": "Scheduled", "league_id": 1,
                 "home_id": 11, "away_id": 12, "home_team": "Home", "away_team": "Away",
                 "kickoff_utc": "2026-11-26T18:00:00Z"}
        home_shots = {"total_shots": {"average": 16, "variance": 2, "samples": 10},
                      "shots_on_target": {"average": 5, "variance": 1, "samples": 10}}
        away_shots = {"total_shots": {"average": 15, "variance": 2, "samples": 10},
                      "shots_on_target": {"average": 5, "variance": 1, "samples": 10}}
        cards = {"yellow_cards": {"average": 3, "variance": 1, "samples": 6},
                 "fouls": {"average": 14, "variance": 4, "samples": 6},
                 "sample_matches": 6, "top_card_players": [("Player A", 2)]}
        output = io.StringIO()
        with patch.object(bot, "API_KEY", "test-key"), patch.object(bot, "DRY_RUN", True), \
             patch.object(bot, "TRACK_ONLY", False), patch.object(bot, "sportapi_today", return_value="2026-11-26"), \
             patch.object(bot, "fixtures_on_date", return_value=[match]), \
             patch.object(bot, "_league_map", return_value={1: "premier league"}), \
             patch.object(bot, "load_models", return_value={}), \
             patch.object(bot, "get_team_shot_profile", side_effect=[home_shots, away_shots]), \
             patch.object(bot, "get_team_card_profile", side_effect=[cards, cards]), \
             patch.object(bot, "record_predictions") as record, patch.object(bot, "build_reports") as reports, \
             patch.object(bot, "_send_telegram") as send, contextlib.redirect_stdout(output):
            bot.main()
        record.assert_not_called()
        reports.assert_not_called()
        send.assert_not_called()
        text = output.getvalue()
        self.assertIn("TIRS TOTAUX", text)
        self.assertIn("TIRS CADRÉS ÉQUIPE", text)
        self.assertIn("CARTONS", text)
        self.assertNotIn("CORNERS", text)
        self.assertNotIn("TIRS CADRÉS JOUEUR", text)
        self.assertIn("aucun message Telegram envoyé", text)

    def test_candidate_model_receives_home_away_form_and_league_in_dry_run(self):
        match = {"id": 56, "status": "Scheduled", "league_id": 1,
                 "home_id": 11, "away_id": 12, "home_team": "Home", "away_team": "Away"}
        shot_profile = {"total_shots": {"average": 16, "variance": 2, "samples": 10},
                        "shots_on_target": {"average": 5, "variance": 1, "samples": 10}}
        form_home = {"shots_for_5": 15.0, "shots_for_10": 14.0}
        form_away = {"shots_for_5": 12.0, "shots_for_10": 13.0}
        output = io.StringIO()
        with patch.object(bot, "API_KEY", "test-key"), patch.object(bot, "DRY_RUN", True), \
             patch.object(bot, "TRACK_ONLY", False), patch.object(bot, "sportapi_today", return_value="2026-11-26"), \
             patch.object(bot, "fixtures_on_date", return_value=[match]), \
             patch.object(bot, "_league_map", return_value={1: "premier league"}), \
             patch.object(bot, "load_models", return_value={"total_tirs": {"fake": True}}), \
             patch.object(bot, "get_team_shot_profile", side_effect=[shot_profile, shot_profile]), \
             patch.object(bot, "get_team_model_form", side_effect=[form_home, form_away]), \
             patch.object(bot, "get_team_card_profile", return_value={}), \
             patch.object(bot, "model_probability", return_value=0.8) as infer, \
             patch.object(bot, "record_predictions") as record, patch.object(bot, "_send_telegram") as send, \
             contextlib.redirect_stdout(output):
            bot.main()
        features = infer.call_args.args[1]
        self.assertEqual(features["league_E0"], 1.0)
        self.assertEqual(features["h_shots_for_5"], 15.0)
        self.assertEqual(features["a_shots_for_5"], 12.0)
        self.assertIn("+27 tirs : 80%", output.getvalue())
        record.assert_not_called()
        send.assert_not_called()

    def test_live_budget_exhaustion_prevents_persistence_and_telegram(self):
        output = io.StringIO()
        usage = {"requests": 38, "limit": 38, "limit_reached": True, "daily_remaining": 20}
        with patch.object(bot, "API_KEY", "test-key"), patch.object(bot, "DRY_RUN", False), \
             patch.object(bot, "TRACK_ONLY", False), patch.object(bot, "request_usage", return_value=usage), \
             patch.object(bot, "sportapi_today", return_value="2026-11-26"), \
             patch.object(bot, "fixtures_on_date", return_value=[self.live_match()]), \
             patch.object(bot, "_league_map", return_value={1: "premier league"}), \
             patch.object(bot, "load_models", return_value={}), \
             patch.object(bot, "get_team_shot_profile", return_value={}), \
             patch.object(bot, "get_team_card_profile", return_value={}), \
             patch.object(bot, "settle_pending") as settle, \
             patch.object(bot, "record_predictions") as record, patch.object(bot, "build_reports") as reports, \
             patch.object(bot, "_send_telegram") as send, \
             patch.dict("os.environ", {"TELEGRAM_TOKEN": "test", "CHAT_ID": "test"}), \
             contextlib.redirect_stdout(output):
            bot.main()
        settle.assert_not_called()
        record.assert_not_called()
        reports.assert_not_called()
        send.assert_not_called()
        self.assertIn("incomplète (budget de requêtes atteint)", output.getvalue())

    def test_live_without_selections_does_not_persist_or_send_empty_message(self):
        output = io.StringIO()
        usage = {"requests": 5, "limit": 100, "limit_reached": False, "daily_remaining": 200}
        with patch.object(bot, "API_KEY", "test-key"), patch.object(bot, "DRY_RUN", False), \
             patch.object(bot, "TRACK_ONLY", False), patch.object(bot, "request_usage", return_value=usage), \
             patch.object(bot, "sportapi_today", return_value="2026-11-26"), \
             patch.object(bot, "fixtures_on_date", return_value=[self.live_match()]), \
             patch.object(bot, "_league_map", return_value={1: "premier league"}), \
             patch.object(bot, "load_models", return_value={}), \
             patch.object(bot, "get_team_shot_profile", return_value={}), \
             patch.object(bot, "get_team_card_profile", return_value={}), \
             patch.object(bot, "settle_pending") as settle, \
             patch.object(bot, "record_predictions") as record, patch.object(bot, "build_reports") as reports, \
             patch.object(bot, "_send_telegram") as send, \
             patch.dict("os.environ", {"TELEGRAM_TOKEN": "test", "CHAT_ID": "test"}), \
             contextlib.redirect_stdout(output):
            bot.main()
        settle.assert_not_called()
        record.assert_not_called()
        reports.assert_not_called()
        send.assert_not_called()
        self.assertIn("Aucune sélection : aucun enregistrement ni message Telegram envoyé", output.getvalue())


if __name__ == "__main__":
    unittest.main()
