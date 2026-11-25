"""Tests unitaires déterministes des helpers métier sans réseau ni stockage."""
import unittest
from unittest.mock import patch

import api_football_validation as api_validation
from api_only_stats import extract
import backtest_bzzoiro as backtest
import tracking
import bot_sportapi
import sportapi_provider as sportapi
from model_rules import is_attacking_player, select_player_sot, select_team_sot
from tracking import _bucket, _number, _prediction_key, _won


class TrackingPureFunctionsTests(unittest.TestCase):
    def test_number_accepts_numeric_strings_and_rejects_invalid_values(self):
        self.assertEqual(_number("3.5"), 3.5)
        self.assertEqual(_number(2), 2.0)
        self.assertIsNone(_number(None))
        self.assertIsNone(_number("n/a"))

    def test_bucket_clamps_and_formats_probability(self):
        self.assertEqual(_bucket(0.0), "00-09%")
        self.assertEqual(_bucket(0.47), "40-49%")
        self.assertEqual(_bucket(1.0), "90-99%")
        self.assertEqual(_bucket(-2), "00-09%")

    def test_won_resolves_market_side_and_direction(self):
        actuals = {"total_tirs": 27.0, "home_sot": 4.0, "away_sot": 3.0,
                   "players": {"p-1": 2.0}}
        row = {"market": "total_tirs", "line": 26.5, "direction": "over"}
        self.assertEqual(_won(row, actuals), (27.0, True))
        self.assertEqual(_won({**row, "direction": "under"}, actuals), (27.0, False))
        self.assertEqual(_won({"market": "tirs_cadres_equipe", "line": 3.5,
                               "side": "home"}, actuals), (4.0, True))
        self.assertEqual(_won({"market": "tirs_cadres_joueur", "line": 1.5,
                               "player_id": "p-1"}, actuals), (2.0, True))

    def test_won_reports_unsettleable_rows(self):
        self.assertEqual(_won({"market": "unknown", "line": 1}, {}), (None, None))
        self.assertEqual(_won({"market": "corners"}, {"corners": 8}), (None, None))
        self.assertEqual(_won({"market": "corners", "line": 8}, {}), (None, None))

    def test_prediction_key_separates_model_versions(self):
        base = {"event_id": 42, "market": "tirs_cadres_equipe", "selection": "home +3.5"}
        old = _prediction_key({**base, "model_version": "v2.4"})
        new = _prediction_key({**base, "model_version": "v2.5"})

        self.assertNotEqual(old, new)

    def test_prediction_key_separates_provider_ids(self):
        base = {"event_id": 42, "market": "total_tirs", "selection": "+27"}
        old = _prediction_key({**base, "source": "bzzoiro"})
        new = _prediction_key({**base, "source": "sportapi"})
        self.assertNotEqual(old, new)

    def test_sportapi_actuals_use_documented_team_fields_only(self):
        payload = {"data": {
            "home": {"shots": 13, "shots_on_target": 6, "yellow_cards": 1},
            "away": {"shots": 11, "shots_on_target": 5, "yellow_cards": 2},
        }}
        actuals = tracking._sportapi_actuals(lambda _: payload, 99)
        self.assertEqual(actuals, {
            "total_tirs": 24.0, "cartons": 3.0,
            "home_sot": 6.0, "away_sot": 5.0,
        })
        self.assertNotIn("corners", actuals)
        self.assertNotIn("players", actuals)

    def test_sportapi_settlement_does_not_call_bzzoiro(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            predictions = data_dir / "predictions.jsonl"
            predictions.write_text(json.dumps({
                "source": "sportapi", "event_id": 101, "market": "total_tirs",
                "selection": "+27", "line": 26.5, "direction": "over", "status": "pending",
            }) + "\n", encoding="utf-8")
            payload = {"data": {
                "home": {"shots": 15, "shots_on_target": 5, "yellow_cards": 1},
                "away": {"shots": 13, "shots_on_target": 4, "yellow_cards": 2},
            }}
            calls = []
            with patch.object(tracking, "DATA_DIR", data_dir), patch.object(
                tracking, "PREDICTIONS_FILE", predictions
            ):
                changed = tracking.settle_pending(
                    lambda _: self.fail("Bzzoiro ne doit pas être appelé"),
                    lambda endpoint: calls.append(endpoint) or payload,
                )
            row = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(changed, 1)
            self.assertEqual(calls, ["fixtures/101/stats"])
            self.assertEqual(row["actual_value"], 28.0)
            self.assertTrue(row["won"])

    def test_reports_do_not_mix_provider_calibration(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            predictions = data_dir / "predictions.jsonl"
            rows = [
                {"source": provider, "market": "total_tirs", "probability": 0.7,
                 "won": True, "status": "settled"}
                for provider in ("bzzoiro", "sportapi")
            ]
            predictions.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with patch.object(tracking, "DATA_DIR", data_dir), \
                 patch.object(tracking, "PREDICTIONS_FILE", predictions), \
                 patch.object(tracking, "REPORT_FILE", data_dir / "report.json"), \
                 patch.object(tracking, "CALIBRATION_FILE", data_dir / "calibration.json"):
                report = tracking.build_reports()
            self.assertEqual(set(report["markets"]), {"bzzoiro:total_tirs", "sportapi:total_tirs"})


class ShotOnTargetModelRulesTests(unittest.TestCase):
    @staticmethod
    def probabilities(values):
        def calculate(mean, variance, line, samples):
            return values[line], 1 - values[line]
        return calculate

    def test_team_selector_uses_production_threshold_and_best_line(self):
        profile = {"average": 11.99, "variance": 10, "samples": 8}
        selected = select_team_sot(profile, self.probabilities({3.5: 0.65, 4.5: 0.72}))
        self.assertEqual(selected, (4.5, 0.72))

    def test_team_selector_rejects_mean_at_cutoff_and_probability_below_cutoff(self):
        probability_fn = self.probabilities({3.5: 0.649, 4.5: 0.64})
        self.assertIsNone(select_team_sot(
            {"average": 12, "variance": 10, "samples": 8}, probability_fn
        ))
        self.assertIsNone(select_team_sot(
            {"average": 11, "variance": 10, "samples": 8}, probability_fn
        ))

    def test_player_selector_enforces_samples_minutes_hit_rate_and_position(self):
        base = {
            "average": 1.2, "variance": 1, "samples": 4,
            "avg_minutes": 55, "hit_rate": 0.55, "position": "Forward",
        }
        probability_fn = self.probabilities({0.5: 0.70, 1.5: 0.65})
        self.assertEqual(select_player_sot(base, probability_fn), (0.5, 0.70))
        for changes in (
            {"samples": 3}, {"avg_minutes": 54.99}, {"hit_rate": 0.49},
            {"position": "Defender"}, {"position": ""},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(select_player_sot({**base, **changes}, probability_fn))

    def test_player_selector_falls_back_to_over_15_only_when_eligible(self):
        profile = {
            "average": 1.2, "variance": 1, "samples": 4,
            "avg_minutes": 60, "hit_rate": 0.50, "position": "AM",
        }
        probability_fn = self.probabilities({0.5: 0.69, 1.5: 0.65})
        self.assertEqual(select_player_sot(profile, probability_fn), (1.5, 0.65))

    def test_position_filter_accepts_attacking_midfielder_not_defender(self):
        self.assertTrue(is_attacking_player("Attacking Midfielder"))
        self.assertFalse(is_attacking_player("Centre Back"))


class BacktestProfileTests(unittest.TestCase):
    def test_team_profile_skips_events_with_stats_explicitly_unavailable(self):
        events = [
            {"id": "unavailable", "home_team_id": 7},
            {"id": "available", "home_team_id": 7},
        ]

        def cached_get(endpoint):
            if endpoint.endswith("/availability/"):
                return {"available": {"stats": endpoint.split("/")[1] != "unavailable"}}
            if endpoint.endswith("/stats/"):
                return {"stats": {"home": {"shots_on_target": 5}}}
            return {}

        with patch.object(backtest, "prior_events", return_value=events), patch.object(
            backtest, "cached_get", side_effect=cached_get
        ):
            result = backtest.profile(7, "home", None, None)

        self.assertEqual(result["shots_on_target"]["samples"], 1)
        self.assertEqual(result["shots_on_target"]["average"], 5.0)

    def test_player_profile_matches_production_eligibility_inputs(self):
        events = [{"id": f"event-{index}"} for index in range(4)]

        def cached_get(endpoint):
            if endpoint.endswith("/availability/"):
                return {"available": {"player_stats": True}}
            if endpoint.endswith("/player-stats/"):
                player_id = endpoint.split("/")[1]
                return {"player_stats": [{
                    "player_id": "p1", "team_id": 7, "minutes_played": 90,
                    "shots_on_target": 1, "position": "Forward",
                }]}
            return {}

        with patch.object(backtest, "prior_events", return_value=events), patch.object(
            backtest, "cached_get", side_effect=cached_get
        ):
            result = backtest.player_profiles(7, None, None)

        self.assertEqual(result["p1"]["samples"], 4)
        self.assertEqual(result["p1"]["avg_minutes"], 90)
        self.assertEqual(result["p1"]["hit_rate"], 1)
        self.assertEqual(result["p1"]["position"], "forward")


class ApiOnlyPureFunctionsTests(unittest.TestCase):
    def test_extract_matches_teams_and_supported_statistic_aliases(self):
        statistics = [
            {"team": {"name": "Home"}, "statistics": [
                {"type": "Total Shots", "value": 12},
                {"type": "Shots on Target", "value": 5},
                {"type": "Corner Kicks", "value": 7},
            ]},
            {"team": {"name": "Away"}, "statistics": [
                {"type": "Shots on Goal", "value": 2},
                {"type": "Yellow Cards", "value": None},
            ]},
        ]
        self.assertEqual(extract(statistics, "Home"), {
            "total_shots": 12, "shots_on_target": 5, "corners": 7,
            "yellow_cards": None, "red_cards": None,
        })
        self.assertEqual(extract(statistics, "Away")["shots_on_target"], 2)
        self.assertEqual(extract(statistics, "Missing"), {})


class ApiFootballDiagnosticTests(unittest.TestCase):
    def test_diagnostic_includes_provider_error_but_redacts_api_key(self):
        secret = "test-api-key-never-print"
        with patch.dict("os.environ", {"API_FOOTBALL_KEY": secret}), patch.object(
            api_validation, "_api_get", side_effect=RuntimeError(f"quota error echoed {secret}")
        ):
            result = api_validation.diagnose_api_football("2026-11-24")

        fixture_result = result["endpoints"]["fixtures"]
        self.assertFalse(fixture_result["ok"])
        self.assertEqual(fixture_result["error"], "RuntimeError")
        self.assertIn("quota error echoed [REDACTED]", fixture_result["message"])
        self.assertNotIn(secret, repr(result))


class SportAPIProviderTests(unittest.TestCase):
    def test_team_profiles_read_only_documented_fields(self):
        fixture = {"id": 12, "home_id": 7, "away_id": 9, "league_id": 4}
        stats = {
            "home": {"shots": 14, "shots_on_target": 6, "fouls": 16, "yellow_cards": 3},
            "away": {"shots": 8, "shots_on_target": 2, "fouls": 10, "yellow_cards": 1},
        }
        events = [
            {"event_type": "yellow_card", "team_side": "home", "player_name": "Player A"},
            {"event_type": "yellow_card", "team_side": "away", "player_name": "Player B"},
        ]
        with patch.object(sportapi, "_prior_fixtures", return_value=[fixture]), patch.object(
            sportapi, "fixture_stats", return_value=stats
        ), patch.object(sportapi, "fixture_events", return_value=events):
            shot = sportapi.get_team_shot_profile.__wrapped__(7, "home", 4, "2026-11-26")
            cards = sportapi.get_team_card_profile.__wrapped__(7, "home", 4, "2026-11-26")
        self.assertEqual(shot["total_shots"]["average"], 14)
        self.assertEqual(shot["shots_on_target"]["average"], 6)
        self.assertEqual(cards["yellow_cards"]["average"], 3)
        self.assertEqual(cards["fouls"]["average"], 16)
        self.assertEqual(cards["top_card_players"], [("Player A", 1)])

    def test_model_form_exposes_weighted_5_and_10_match_features(self):
        fixtures = [
            {"id": 12, "home_id": 7, "away_id": 9},
            {"id": 11, "home_id": 8, "away_id": 7},
        ]
        stats = {
            12: {"home": {"shots": 14, "shots_on_target": 6, "fouls": 16, "yellow_cards": 3},
                 "away": {"shots": 8, "shots_on_target": 2, "fouls": 10, "yellow_cards": 1}},
            11: {"home": {"shots": 10, "shots_on_target": 4, "fouls": 12, "yellow_cards": 2},
                 "away": {"shots": 7, "shots_on_target": 3, "fouls": 9, "yellow_cards": 1}},
        }
        with patch.object(sportapi, "_prior_fixtures", return_value=fixtures), patch.object(
            sportapi, "fixture_stats", side_effect=lambda fid: stats[fid]
        ):
            result = sportapi.get_team_model_form.__wrapped__(7, "home", 4, "2026-11-26")
        self.assertAlmostEqual(result["shots_for_5"], (14 * 2.96 + 7 * 0.962) / (2.96 + 0.962))
        self.assertEqual(result["shots_for_10"], result["shots_for_5"])
        self.assertAlmostEqual(result["shots_against_5"], (8 * 2.96 + 10 * 0.962) / (2.96 + 0.962))
        self.assertIn("yellow_for_10", result)
        self.assertIn("fouls_against_5", result)


class SportAPIModelInferenceTests(unittest.TestCase):
    def test_exported_logistic_inference_uses_scaled_features_and_missing_indicators(self):
        model = {"features": ["a", "b"], "medians": [5.0, 10.0],
                 "indicator_indices": [1], "scaler_mean": [0.0, 0.0, 0.0],
                 "scaler_scale": [1.0, 1.0, 1.0], "coefficients": [0.0, 0.0, 2.0],
                 "intercept": 0.0}
        probability = bot_sportapi.model_probability(model, {"a": 2.0, "b": None})
        self.assertAlmostEqual(probability, 1 / (1 + __import__("math").exp(-2)))

    def test_uncovered_competitions_get_only_supported_league_flags(self):
        flags = bot_sportapi._league_features("F1")
        self.assertEqual(flags["league_F1"], 1.0)
        self.assertEqual(sum(flags.values()), 1.0)

    def test_allowed_competitions_prioritize_major_leagues_and_uefa_cups(self):
        for name in ("Premier League", "UEFA Champions League", "Europa League",
                     "UEFA Conference League", "Ligue 1"):
            with self.subTest(name=name):
                self.assertTrue(bot_sportapi.is_allowed_league(name))
        for name in ("Eredivisie", "Primeira Liga", "Championship", "Ligue 2", "Serie B"):
            with self.subTest(name=name):
                self.assertFalse(bot_sportapi.is_allowed_league(name))


if __name__ == "__main__":
    unittest.main()
