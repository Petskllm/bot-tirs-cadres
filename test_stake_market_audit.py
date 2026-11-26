import unittest
from datetime import datetime, timedelta, timezone

from stake_market_audit import (
    StakeApiError,
    StakeSportsbookClient,
    competition_label,
    render_markdown,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        for suffix, payload in self.responses:
            if url.endswith(suffix):
                if callable(payload):
                    payload = payload(params or {})
                return FakeResponse(payload)
        raise AssertionError(f"Unexpected URL: {url}")


def page(data, has_next=False, next_offset=None):
    return {"data": data, "pagination": {
        "hasNextPage": has_next, "nextOffset": next_offset,
    }}


class StakeMarketAuditTests(unittest.TestCase):
    def test_competition_filter_keeps_top_five_and_uefa_only(self):
        self.assertEqual(competition_label({"name": "Premier League", "category": "England"}), "premier league")
        self.assertEqual(competition_label({"name": "UEFA Europa League", "category": "Europe"}), "europa league")
        self.assertIsNone(competition_label({"name": "Premier League", "category": "India"}))
        self.assertIsNone(competition_label({"name": "UEFA Youth League", "category": "Europe"}))

    def test_missing_key_fails_before_any_network_call(self):
        session = FakeSession([])
        with self.assertRaisesRegex(StakeApiError, "aucun appel Stake effectué"):
            StakeSportsbookClient("", session=session)
        self.assertEqual(session.calls, [])

    def test_market_inventory_collects_open_offers_and_team_props(self):
        now = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
        starts = (now + timedelta(hours=3)).isoformat()
        fixtures = [{
            "id": "m-1", "name": "Home - Away", "status": "upcoming",
            "startsAt": starts, "props": True,
            "offers": [
                {"name": "Total Shots", "status": "open", "selections": [
                    {"name": "Over", "line": 26.5, "available": True, "decimalOdds": 1.8},
                    {"name": "Under", "line": 26.5, "available": False, "decimalOdds": None},
                ]},
                {"name": "Total Cards", "status": "suspended", "selections": [
                    {"name": "Over", "line": 4.5, "available": True, "decimalOdds": 1.9},
                ]},
                {"name": "Unused Market", "status": "open", "selections": [
                    {"name": "Over", "line": 1.5, "available": False, "decimalOdds": None},
                ]},
            ],
        }]
        responses = [
            ("/en/sports", page([{"id": "football", "name": "Football"}])),
            ("/en/sports/football/tournaments", page([
                {"id": "epl", "name": "Premier League", "category": "England"},
                {"id": "ind", "name": "Premier League", "category": "India"},
                {"id": "ucl", "name": "UEFA Champions League", "category": "Europe"},
            ])),
            ("/en/sports/football/tournaments/epl/matches", page(fixtures)),
            ("/en/sports/football/tournaments/ucl/matches", page([])),
            ("/props", page([{
                "type": "team", "market": "Shots on Target", "competitor": "Home",
                "team": None, "lines": [{"line": 3.5, "selections": [
                    {"name": "over", "available": True, "decimalOdds": 1.7},
                    {"name": "under", "available": False, "decimalOdds": None},
                ]}],
            }])),
        ]
        session = FakeSession(responses)
        client = StakeSportsbookClient("test-key", session=session)
        inventory = client.upcoming_market_inventory(days_ahead=7, now=now)

        self.assertEqual(len(inventory["fixtures"]), 1)
        self.assertEqual(inventory["competitions"], ["champions league", "premier league"])
        by_name = {(market["source"], market["name"]): market for market in inventory["markets"]}
        self.assertEqual(by_name[("offer", "Total Shots")]["fixture_count"], 1)
        self.assertNotIn(("offer", "Total Cards"), by_name)
        self.assertNotIn(("offer", "Unused Market"), by_name)
        self.assertEqual(by_name[("team_prop", "Shots on Target")]["samples"][0]["line"], 3.5)
        self.assertEqual(inventory["request_count"], len(session.calls))
        self.assertEqual(session.headers["X-API-Key"], "test-key")
        report = render_markdown(inventory)
        self.assertIn("Shots on Target", report)
        self.assertNotIn("test-key", report)

    def test_pages_uses_server_next_offset(self):
        responses = [
            ("/catalog", lambda params: page([{"id": params["offset"]}], True, 30)
             if params["offset"] == 0 else page([{"id": 30}])),
        ]
        session = FakeSession(responses)
        client = StakeSportsbookClient("key", session=session)
        rows = client.pages("/catalog", {"limit": 30})
        self.assertEqual(rows, [{"id": 0}, {"id": 30}])
        self.assertEqual([call[1]["offset"] for call in session.calls], [0, 30])


if __name__ == "__main__":
    unittest.main()
