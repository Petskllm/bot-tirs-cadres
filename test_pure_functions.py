"""Tests unitaires déterministes des helpers métier sans réseau ni stockage."""
import unittest

from api_only_stats import extract
from tracking import _bucket, _number, _won


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


if __name__ == "__main__":
    unittest.main()
