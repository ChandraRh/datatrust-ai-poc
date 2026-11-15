"""Built-in check settings (web/builtin.py): defaults, saved defaults, one pipeline's override, and the checks that obey them."""
import unittest
from unittest.mock import patch

import builtin


def saved(*rows):
    return {(r["check_key"], r["target"]): r for r in rows}


def row(key, target="", enabled=None, severity=None, **params):
    return {"check_key": key, "target": target, "enabled": enabled, "severity": severity, "params": params}


class SettingTests(unittest.TestCase):
    def test_defaults_when_nothing_is_saved(self):
        with patch.object(builtin, "_load", return_value={}):
            self.assertEqual(builtin.setting("adf.volume"), {"enabled": True, "severity": "MEDIUM", "low_pct": 50, "min_history": 3})
            self.assertEqual(builtin.setting("poc.settlement")["severity"], "CRITICAL")

    def test_a_pipeline_override_wins_over_the_saved_default(self):
        rows = saved(row("adf.volume", severity="HIGH"),                                   # all pipelines
                     row("adf.volume", "adf:3:PL_01_File_Monitoring", low_pct=5))           # one pipeline
        with patch.object(builtin, "_load", return_value=rows):
            self.assertEqual(builtin.setting("adf.volume", 3, "PL_02_Data_Processing")["low_pct"], 50)
            pl1 = builtin.setting("adf.volume", 3, "PL_01_File_Monitoring")
            self.assertEqual((pl1["low_pct"], pl1["severity"]), (5, "HIGH"))           # its own limit, the default's severity

    def test_switched_off(self):
        with patch.object(builtin, "_load", return_value=saved(row("poc.duplicates", enabled=False))):
            self.assertFalse(builtin.setting("poc.duplicates")["enabled"])

    def test_bad_values_are_refused(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 99"):
            builtin._clean("adf.volume", None, None, {"low_pct": 150})
        with self.assertRaisesRegex(ValueError, "severity"):
            builtin._clean("adf.volume", None, "URGENT", {})
        with self.assertRaisesRegex(ValueError, "no setting"):
            builtin._clean("poc.settlement", None, None, {"hours": 3})
        with self.assertRaisesRegex(ValueError, "unknown"):
            builtin._clean("adf.telepathy", None, None, {})

    def test_levels(self):
        self.assertEqual((builtin.lower("HIGH"), builtin.lower("LOW"), builtin.higher("MEDIUM"), builtin.higher("CRITICAL")),
                         ("MEDIUM", "LOW", "HIGH", "CRITICAL"))


class AdfObeysTheSettings(unittest.TestCase):
    """The ADF volume check reads its limit and severity from the settings, for incidents and for the score alike."""
    SRC = {"source_id": 3, "name": "f", "config": {}}

    def runs(self, rows_now, earlier):
        mk = lambda i, n: {"run_id": f"r{i}", "status": "Succeeded", "run_start": "2026-11-14T10:00:00+00:00",
                           "run_end": "2026-11-14T10:05:00+00:00", "duration_ms": 60000, "rows_written": n, "rows_skipped": 0}
        return [mk(0, rows_now)] + [mk(i + 1, n) for i, n in enumerate(earlier)]

    def issues(self, rows, runs):
        from adf import insights
        with patch.object(builtin, "_load", return_value=rows), patch.object(insights, "_tasks", return_value=[]), \
                patch.object(insights, "_checks", return_value=[]), patch.object(insights, "_triggers_for", return_value=[]):
            found = insights._pipeline_issues(self.SRC, "PL_01", runs, {})
            sig = insights.pipeline_signals(self.SRC, "PL_01", runs, found, {})
        return found, sig

    def test_too_few_rows_by_default_but_not_with_a_lower_limit(self):
        runs = self.runs(100, [250, 260, 240])                      # 40 % of usual
        found, sig = self.issues({}, runs)
        self.assertEqual([(i["type"], i["severity"]) for i in found], [("VOLUME", "MEDIUM")])
        found, sig = self.issues(saved(row("adf.volume", "adf:3:PL_01", low_pct=5)), runs)
        self.assertEqual(found, [])
        self.assertIn("PL_01: usual number of rows loaded", [s["text"] for s in sig])

    def test_switched_off_means_no_incident_and_no_signal(self):
        found, sig = self.issues(saved(row("adf.volume", enabled=False)), self.runs(0, [250, 260, 240]))
        self.assertEqual(found, [])
        self.assertFalse([s for s in sig if s["subject"] == "volume"])

    def test_zero_rows_is_one_level_above_the_setting(self):
        found, _ = self.issues(saved(row("adf.volume", severity="HIGH")), self.runs(0, [250, 260, 240]))
        self.assertEqual(found[0]["severity"], "CRITICAL")


if __name__ == "__main__":
    unittest.main()
