"""'Retry failed batch' starts the POC pipeline in Azure by adding a message to its upload queue."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline_trigger  # noqa: E402


class FakeQueue:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_message(self, text):
        if self.fail:
            raise PermissionError("no role")
        self.sent.append(json.loads(text))


URL = "https://lake.queue.core.windows.net/adls-ready"


class PipelineTriggerTests(unittest.TestCase):
    def test_local_install_sends_nothing(self):
        with patch.dict(os.environ, {"PIPELINE_TRIGGER_QUEUE_URL": ""}), \
             patch.object(pipeline_trigger, "_queue_client") as client:
            result = pipeline_trigger.start_pipeline("2026-11-02", "admin")
        client.assert_not_called()
        self.assertFalse(result["pipeline_started"])
        self.assertIn("local pipeline worker", result["pipeline_note"])

    def test_azure_sends_one_message(self):
        queue = FakeQueue()
        with patch.dict(os.environ, {"PIPELINE_TRIGGER_QUEUE_URL": URL}), \
             patch.object(pipeline_trigger, "_queue_client", return_value=queue) as client:
            result = pipeline_trigger.start_pipeline("2026-11-02", "admin")
        client.assert_called_once_with(URL)
        self.assertTrue(result["pipeline_started"])
        self.assertEqual(len(queue.sent), 1)
        self.assertEqual(queue.sent[0]["batch"], "2026-11-02")
        self.assertEqual(queue.sent[0]["requested_by"], "admin")

    def test_failure_is_reported_not_raised(self):
        with patch.dict(os.environ, {"PIPELINE_TRIGGER_QUEUE_URL": URL}), \
             patch.object(pipeline_trigger, "_queue_client", return_value=FakeQueue(fail=True)):
            result = pipeline_trigger.start_pipeline("2026-11-02", "admin")
        self.assertFalse(result["pipeline_started"])
        self.assertIn("PermissionError", result["pipeline_note"])
        self.assertIn("daily run", result["pipeline_note"])


if __name__ == "__main__":
    unittest.main()
