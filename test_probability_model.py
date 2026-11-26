import math
import unittest

from probability import line_probabilities, score_probabilities


class CountProbabilityTests(unittest.TestCase):
    def test_poisson_limit_is_complementary_on_half_line(self):
        over, under = line_probabilities(2.0, 2.0, 0.5, 20)
        self.assertAlmostEqual(over, 1.0 - math.exp(-2.0))
        self.assertAlmostEqual(under, math.exp(-2.0))
        self.assertAlmostEqual(over + under, 1.0)

    def test_overdispersion_produces_valid_probabilities(self):
        over, under = line_probabilities(10.0, 35.0, 9.5, 8)
        self.assertGreaterEqual(over, 0.0)
        self.assertLessEqual(over, 1.0)
        self.assertAlmostEqual(over + under, 1.0)

    def test_lower_mean_makes_over_probability_smaller(self):
        low, _ = line_probabilities(3.0, 3.0, 4.5, 12)
        high, _ = line_probabilities(6.0, 6.0, 4.5, 12)
        self.assertLess(low, high)

    def test_zero_mean_has_no_over_probability(self):
        self.assertEqual(line_probabilities(0.0, 0.0, 0.5, 10), (0.0, 1.0))

    def test_invalid_dispersion_is_rejected(self):
        with self.assertRaises(ValueError):
            line_probabilities(3.0, -1.0, 0.5, 5)


class ProbabilityScoringTests(unittest.TestCase):
    def test_reports_brier_log_loss_and_calibration_gap(self):
        report = score_probabilities([
            {"probability": 0.8, "won": True},
            {"probability": 0.3, "won": False},
        ])
        self.assertEqual(report["predictions"], 2)
        self.assertEqual(report["wins"], 1)
        self.assertEqual(report["hit_rate"], 0.5)
        self.assertEqual(report["average_probability"], 0.55)
        self.assertEqual(report["calibration_gap"], -0.05)
        self.assertGreater(report["brier_score"], 0)
        self.assertGreater(report["log_loss"], 0)

    def test_empty_backtest_has_no_fake_calibration(self):
        self.assertEqual(score_probabilities([]), {"predictions": 0})


if __name__ == "__main__":
    unittest.main()
