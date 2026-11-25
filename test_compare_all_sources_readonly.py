import unittest
from unittest.mock import patch

import compare_all_sources_readonly as audit


class FakeResponse:
    def __init__(self, payload, headers=None, status=200):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status

    def json(self):
        return self._payload


class ReadOnlyThreeSourceAuditTests(unittest.TestCase):
    def test_normalizes_accents_and_punctuation_for_team_pair_matching(self):
        self.assertEqual(audit.normalize("Étoile FC & Co."), "etoile fc co")

    def test_compares_same_finished_match_without_writing_data(self):
        sport_stats = {"success": True, "available": True, "data": {
            "home": {"shots": 12, "shots_on_target": 4, "yellow_cards": 2, "fouls": 10},
            "away": {"shots": 9, "shots_on_target": 3, "yellow_cards": 1, "fouls": 14},
        }}
        api_stats = {"response": [
            {"team": {"name": "Home FC"}, "statistics": [
                {"type": "Total Shots", "value": 12}, {"type": "Shots on Goal", "value": 4},
                {"type": "Yellow Cards", "value": 2}, {"type": "Fouls", "value": 10},
            ]},
            {"team": {"name": "Away FC"}, "statistics": [
                {"type": "Total Shots", "value": 9}, {"type": "Shots on Goal", "value": 3},
                {"type": "Yellow Cards", "value": 1}, {"type": "Fouls", "value": 14},
            ]},
        ]}
        bzz_stats = {"stats": {
            "home": {"total_shots": 13, "shots_on_target": 4, "yellow_cards": 2, "fouls": 10},
            "away": {"total_shots": 9, "shots_on_target": 3, "yellow_cards": 1, "fouls": 14},
        }}
        def fake_get(url, headers=None, params=None, **kwargs):
            if url.endswith("/fixtures/date/2026-11-24"):
                return FakeResponse({"success": True, "data": [{
                    "id": 101, "status": "FT", "home_team": "Home FC", "away_team": "Away FC",
                }]}, {"X-RateLimit-Remaining": "19"})
            if url.endswith("/fixtures"):
                return FakeResponse({"response": [{
                    "fixture": {"id": 201, "status": {"short": "FT"}},
                    "teams": {"home": {"name": "Home FC"}, "away": {"name": "Away FC"}},
                }]}, {"x-ratelimit-requests-remaining": "98"})
            if url.endswith("/events/"):
                return FakeResponse({"results": [{
                    "id": 301, "status": "finished", "home_team": "Home FC", "away_team": "Away FC",
                }]})
            if url.endswith("/fixtures/101/stats"):
                return FakeResponse(sport_stats, {"X-RateLimit-Remaining": "18"})
            if url.endswith("/fixtures/statistics"):
                return FakeResponse(api_stats, {"x-ratelimit-requests-remaining": "97"})
            if url.endswith("/events/301/stats/"):
                return FakeResponse(bzz_stats)
            self.fail(f"Unexpected URL: {url}")
        with patch.dict("os.environ", {
            "SPORTAPI_KEY": "s-test", "API_FOOTBALL_KEY": "f-test", "BZZOIRO_TOKEN": "b-test",
        }), patch.object(audit.requests, "get", side_effect=fake_get) as request:
            result = audit.compare("2026-11-24", 1)
        self.assertEqual(request.call_count, 6)
        self.assertEqual(result["common_finished_matches"], 1)
        self.assertEqual(result["sampled_match_count"], 1)
        self.assertEqual(result["coverage_and_requests_by_source"]["sportapi"]["complete_matches"], 1)
        pair = result["pairwise_field_agreement"]["api_football_vs_sportapi"]["total_shots"]
        self.assertEqual(pair["pairs"], 2)
        self.assertEqual(pair["exact"], 2)
        self.assertEqual(pair["within_1"], 2)
        self.assertEqual(result["mode"], "GET-only; audit only; no prediction, persistence, or Telegram")

    def test_global_get_cap_is_enforced(self):
        client = audit.ReadOnlyAudit("s", "", "", max_gets=1)
        with patch.object(audit.requests, "get", return_value=FakeResponse({"success": True}, {"X-RateLimit-Remaining": "20"})) as request:
            self.assertEqual(client.get("sportapi", "https://example.test/one", {}), {"success": True})
            with self.assertRaises(audit.AuditStop):
                client.get("sportapi", "https://example.test/two", {})
        self.assertEqual(request.call_count, 1)

    def test_missing_sportapi_quota_header_stops_followup_requests(self):
        client = audit.ReadOnlyAudit("s", "", "")
        with patch.object(audit.requests, "get", return_value=FakeResponse({"success": True})):
            self.assertEqual(client.get("sportapi", "https://example.test/one", {}), {"success": True})
            with self.assertRaisesRegex(audit.AuditStop, "quota journalier non vérifiable"):
                client.get("sportapi", "https://example.test/two", {})

    def test_network_errors_are_counted_and_do_not_trigger_retries(self):
        client = audit.ReadOnlyAudit("", "f", "")
        with patch.object(audit.requests, "get", side_effect=audit.requests.ConnectionError("offline")) as request:
            self.assertIsNone(client.get("api_football", "https://example.test", {}))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(client.calls["api_football"], 1)
        self.assertEqual(client.errors["api_football"], 1)


if __name__ == "__main__":
    unittest.main()
