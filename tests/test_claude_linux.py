"""Claude on a Linux host: login from a file or a setup-token, meter from headers.

No network: the header parser is pure and the credential lookups read a temp
HOME.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_usage_module  # noqa: E402

HEADERS = {
    "Anthropic-Ratelimit-Unified-5h-Utilization": "0.21",
    "Anthropic-Ratelimit-Unified-5h-Reset": "1790311200",
    "Anthropic-Ratelimit-Unified-7d-Utilization": "0.48",
    "Anthropic-Ratelimit-Unified-7d-Reset": "1790496000",
    "Anthropic-Ratelimit-Unified-Status": "allowed",
}


class ClaudeHeaderMeterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_headers_become_the_usage_payload(self):
        d = self.m.claude_usage_from_headers(HEADERS)
        self.assertAlmostEqual(d["five_hour"]["utilization"], 21.0)
        self.assertAlmostEqual(d["seven_day"]["utilization"], 48.0)
        self.assertEqual(d["five_hour"]["resets_at"], "2026-09-25T04:40:00Z")

    def test_header_payload_parses_into_windows(self):
        st = self.m.parse_claude_usage(self.m.claude_usage_from_headers(HEADERS), None)
        names = {w.name: w.used_pct for w in st.windows}
        self.assertAlmostEqual(names["5h_session"], 21.0)
        self.assertAlmostEqual(names["weekly_all"], 48.0)

    def test_no_headers_is_no_meter(self):
        self.assertEqual(self.m.claude_usage_from_headers({}), {})
        bad = {"anthropic-ratelimit-unified-5h-utilization": "n/a"}
        self.assertEqual(self.m.claude_usage_from_headers(bad), {})


class ClaudeCredentialSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_credentials_file_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude"))
            with open(os.path.join(d, ".claude", ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"accessToken": "tok-file"}}, f)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(d, ".claude")}):
                c = self.m._file_claude_creds()
        self.assertEqual(c["claudeAiOauth"]["accessToken"], "tok-file")

    def test_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                self.assertIsNone(self.m._file_claude_creds())

    def test_env_token(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": " tok-env "}):
            self.assertEqual(self.m._env_claude_creds()["claudeAiOauth"]["accessToken"], "tok-env")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}):
            self.assertIsNone(self.m._env_claude_creds())


if __name__ == "__main__":
    unittest.main()
