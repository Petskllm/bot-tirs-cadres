import unittest

from sportapi_dry_run_preflight import choose_budget, remaining_from_headers, DAILY_NAMES, MONTHLY_NAMES


class DryRunPreflightTests(unittest.TestCase):
    def test_parses_daily_and_monthly_headers_case_insensitively(self):
        headers = {"X-RateLimit-Remaining": "161", "X-RateLimit-Remaining-Monthly": "5300"}
        self.assertEqual(remaining_from_headers(headers, DAILY_NAMES), 161)
        self.assertEqual(remaining_from_headers(headers, MONTHLY_NAMES), 5300)

    def test_uses_all_reported_remaining_quota_without_an_artificial_run_cap(self):
        self.assertEqual(choose_budget(2000), 2000)
        self.assertEqual(choose_budget(1897), 1897)
        self.assertEqual(choose_budget(161), 161)
        self.assertEqual(choose_budget(40), 40)
        self.assertEqual(choose_budget(1), 1)
        self.assertEqual(choose_budget(2000, 1700), 1700)

    def test_rejects_unknown_or_low_daily_quota(self):
        for remaining in (None, 0):
            with self.subTest(remaining=remaining), self.assertRaises(ValueError):
                choose_budget(remaining)
        self.assertEqual(choose_budget(39), 39)

    def test_rejects_low_monthly_quota_if_reported(self):
        with self.assertRaises(ValueError):
            choose_budget(500, 0)
        self.assertEqual(choose_budget(500, 39), 39)


if __name__ == "__main__":
    unittest.main()
