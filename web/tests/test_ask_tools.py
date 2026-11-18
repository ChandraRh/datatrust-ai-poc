import collections
import unittest
from unittest.mock import MagicMock, patch

import agent
import ask
import llm


class AskToolTests(unittest.TestCase):
    def tools_sent(self, poc: bool) -> list:
        """The tool list ask() hands to the model for a POC / non-POC source."""
        sent = {}

        def fake_agent(question, history, tools, runner, system):
            sent["tools"] = tools
            return agent.reply("ok")
        with patch.object(ask.scopes, "resolve", return_value={"key": "db:1", "name": "POC", "type": "PostgreSQL", "poc": poc}), \
                patch.object(ask.scopes, "list_scopes", return_value=[]), \
                patch.object(llm, "provider", return_value="gemini"), \
                patch.object(agent, "llm_agent", side_effect=fake_agent):
            ask.ask("Revenue by region")
        return sent["tools"]

    def test_no_tool_is_declared_twice(self):
        # Gemini rejects the whole request (400 "Duplicate function declaration") when one name appears twice
        for poc in (True, False):
            names = [t[0] for t in self.tools_sent(poc)]
            self.assertEqual([n for n, c in collections.Counter(names).items() if c > 1], [], f"poc={poc}")

    def test_poc_source_still_gets_its_revenue_tools(self):
        names = {t[0] for t in self.tools_sent(True)}
        self.assertTrue({t[0] for t in agent.TOOLS} <= names)

    def test_a_400_shows_the_reason_the_api_gave(self):
        resp = MagicMock(status_code=400, text="")
        resp.json.return_value = {"error": {"message": "Duplicate function declaration found: get_incidents"}}
        err = Exception("400 Client Error")
        err.response = resp
        self.assertIn("Duplicate function declaration found: get_incidents", llm.describe_error(err))


if __name__ == "__main__":
    unittest.main()
