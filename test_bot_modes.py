import unittest

from bot_modes import should_persist_results, should_send_telegram


class BotModeTests(unittest.TestCase):
    def test_dry_run_does_not_persist_or_send(self):
        self.assertFalse(should_persist_results(True, False))
        self.assertFalse(should_send_telegram(True, False))

    def test_track_only_persists_without_sending_even_if_dry_run_is_false(self):
        self.assertTrue(should_persist_results(False, True))
        self.assertFalse(should_send_telegram(False, True))

    def test_track_only_works_with_default_dry_run(self):
        self.assertTrue(should_persist_results(True, True))
        self.assertFalse(should_send_telegram(True, True))

    def test_live_mode_persists_and_sends(self):
        self.assertTrue(should_persist_results(False, False))
        self.assertTrue(should_send_telegram(False, False))


if __name__ == "__main__":
    unittest.main()
