"""Tests de provenance du règlement API-Football, entièrement hors ligne."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tracking


class ApiFootballSettlementTests(unittest.TestCase):
    def test_api_football_rows_use_only_api_football_actuals(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            predictions = data_dir / "predictions.jsonl"
            row = {
                "source": "api_football", "event_id": 501, "market": "total_tirs",
                "selection": "+27", "line": 26.5, "direction": "over", "status": "pending",
            }
            predictions.write_text(json.dumps(row) + "\n", encoding="utf-8")
            calls = []
            with patch.object(tracking, "DATA_DIR", data_dir), patch.object(
                tracking, "PREDICTIONS_FILE", predictions
            ):
                changed = tracking.settle_pending(
                    sportapi_getter=lambda _: self.fail("SportAPI ne doit pas régler une ligne API-Football"),
                    api_football_getter=lambda event_id: calls.append(event_id) or {"total_tirs": 29.0},
                )
            stored = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(changed, 1)
        self.assertEqual(calls, [501])
        self.assertEqual(stored["source"], "api_football")
        self.assertEqual(stored["actual_value"], 29.0)
        self.assertTrue(stored["won"])

    def test_api_football_rows_stay_pending_without_api_football_getter(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            predictions = data_dir / "predictions.jsonl"
            row = {"source": "api_football", "event_id": 502, "market": "cartons",
                   "line": 4.5, "status": "pending"}
            predictions.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with patch.object(tracking, "DATA_DIR", data_dir), patch.object(
                tracking, "PREDICTIONS_FILE", predictions
            ):
                changed = tracking.settle_pending(sportapi_getter=lambda _: {})
            stored = json.loads(predictions.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(changed, 0)
        self.assertEqual(stored, row)


if __name__ == "__main__":
    unittest.main()
