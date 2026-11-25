import unittest

from sportapi_dry_run_preflight import choose_budget, remaining_from_headers, DAILY_NAMES, MONTHLY_NAMES


class DryRunPreflightTests(unittest.TestCase):
    def test_parses_daily_and_monthly_headers_case_insensitively(self):
        headers = {"X-RateLimit-Remaining": "161", "X-RateLimit-Remaining-Monthly": "5300"}
        self.assertEqual(remaining_from_headers(headers, DAILY_NAMES), 161)
        self.assertEqual(remaining_from_headers(headers, MONTHLY_NAMES), 5300)

    def test_caps_bot_and_preserves_twenty_request_buffer(self):
        self.assertEqual(choose_budget(161), 100)
        self.assertEqual(choose_budget(130), 100)
        self.assertEqual(choose_budget(120), 100)
        self.assertEqual(choose_budget(59), 39)
        self.assertEqual(choose_budget(40), 20)

    def test_rejects_unknown_or_low_daily_quota(self):
        for remaining in (None, 0, 39):
            with self.subTest(remaining=remaining), self.assertRaises(ValueError):
                choose_budget(remaining)

    def test_rejects_low_monthly_quota_if_reported(self):
        with self.assertRaises(ValueError):
            choose_budget(500, 39)


if __name__ == "__main__":
    unittest.main()
