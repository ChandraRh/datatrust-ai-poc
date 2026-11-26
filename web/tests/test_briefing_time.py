"""The daily Chat briefing is posted after BRIEFING_CHAT_AT in BRIEFING_TZ (web/alerts.py), not in the server's UTC clock."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import alerts


def settings(values):
    return patch.object(alerts, "_setting", side_effect=lambda name, default="": values.get(name, default))


class BriefingTimeTests(unittest.TestCase):
    UTC_0329 = datetime(2026, 11, 27, 3, 29, tzinfo=timezone.utc)      # 08:59 in India
    UTC_0331 = datetime(2026, 11, 27, 3, 31, tzinfo=timezone.utc)      # 09:01 in India

    def test_the_time_zone_moves_the_clock(self):
        with settings({"BRIEFING_TZ": "Asia/Kolkata"}):
            self.assertEqual(alerts.briefing_now(self.UTC_0331).strftime("%H:%M"), "09:01")

    def test_without_a_time_zone_the_server_clock_is_used(self):
        with settings({}):
            self.assertEqual(alerts.briefing_now(self.UTC_0331), self.UTC_0331)

    def test_nothing_is_posted_before_nine_in_india(self):
        with settings({"BRIEFING_CHAT_AT": "09:00", "BRIEFING_TZ": "Asia/Kolkata"}), patch("appdb.connect") as db:
            self.assertEqual(alerts.daily_briefing(self.UTC_0329), [])
        db.assert_not_called()

    def test_after_nine_in_india_each_source_is_posted(self):
        with settings({"BRIEFING_CHAT_AT": "09:00", "BRIEFING_TZ": "Asia/Kolkata"}), patch("appdb.connect") as db, \
                patch("scopes.list_scopes", return_value=[{"key": "db:1"}]), patch("ai.briefing", return_value={}), \
                patch.object(alerts, "send") as send:
            db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = ("BRIEFING:db:1:2026-11-27",)
            self.assertEqual(alerts.daily_briefing(self.UTC_0331), ["db:1"])
        key = db.return_value.__enter__.return_value.execute.call_args_list[0][0][1][0]
        self.assertEqual(key, "BRIEFING:db:1:2026-11-27")                 # the India date, posted once per day
        send.assert_called_once()

    def test_no_time_set_means_no_briefing(self):
        with settings({}), patch("appdb.connect") as db:
            self.assertEqual(alerts.daily_briefing(self.UTC_0331), [])
        db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
