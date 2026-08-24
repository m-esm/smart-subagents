"""Grok OAuth refresh: expire detection, token apply, 401 retry.

No live network. check_grok is exercised with _http_json / _grok_oidc_refresh
monkeypatched so a stale bearer never reaches the billing host.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import load_usage_module  # noqa: E402


class GrokRefreshHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_usage_module()

    def test_expired_when_missing_or_past(self):
        now = 1_700_000_000.0
        self.assertTrue(self.m._grok_access_expired({}, now=now))
        self.assertTrue(self.m._grok_access_expired({"expires_at": "not-a-date"}, now=now))
        self.assertTrue(
            self.m._grok_access_expired(
                {"expires_at": "2023-01-01T00:00:00Z"}, now=now
            )
        )

    def test_fresh_when_well_inside_window(self):
        now = 1_700_000_000.0
        # now+3600s
        self.assertFalse(
            self.m._grok_access_expired(
                {"expires_at": "2023-11-14T23:08:20Z"}, now=now, skew=90
            )
        )

    def test_skew_treats_near_expiry_as_expired(self):
        now = 1_700_000_000.0
        # 30s from now
        self.assertTrue(
            self.m._grok_access_expired(
                {"expires_at": "2023-11-14T22:13:50Z"}, now=now, skew=90
            )
        )

    def test_apply_token_response_rotates_key_and_refresh(self):
        entry = {"key": "old", "refresh_token": "r1", "expires_at": "old"}
        out = self.m._apply_grok_token_response(
            entry,
            {"access_token": "new-access", "refresh_token": "r2", "expires_in": 3600},
            now=1_700_000_000.0,
        )
        self.assertIs(out, entry)
        self.assertEqual(entry["key"], "new-access")
        self.assertEqual(entry["refresh_token"], "r2")
        self.assertTrue(entry["expires_at"].startswith("2023-11-14T"))

    def test_apply_keeps_refresh_when_issuer_omits_it(self):
        entry = {"key": "old", "refresh_token": "keep-me"}
        self.m._apply_grok_token_response(entry, {"access_token": "n"})
        self.assertEqual(entry["refresh_token"], "keep-me")

    def test_pick_entry_skips_non_dicts(self):
        auth = {"meta": "x", "https://auth.x.ai::id": {"key": "k", "email": "a@b"}}
        self.assertEqual(self.m._grok_pick_entry(auth)["key"], "k")
        self.assertIsNone(self.m._grok_pick_entry({"x": {}}))


class GrokCheckRefresh(unittest.TestCase):
    def setUp(self):
        self.m = load_usage_module()
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        grok = home / ".grok"
        grok.mkdir()
        self.auth_path = grok / "auth.json"
        self.auth_path.write_text(
            json.dumps(
                {
                    "https://auth.x.ai::id": {
                        "key": "stale-token",
                        "refresh_token": "rt",
                        "oidc_client_id": "cid",
                        "oidc_issuer": "https://auth.x.ai",
                        "email": "m@x.test",
                        "expires_at": "2020-01-01T00:00:00Z",
                    }
                }
            )
        )
        self.m.HOME = home
        self.calls = []

        def fake_http(url, headers, method="GET", data=None):
            self.calls.append((method, url, headers.get("Authorization", "")))
            if url.endswith("/oauth2/token"):
                return 200, {
                    "access_token": "fresh-token",
                    "refresh_token": "rt2",
                    "expires_in": 3600,
                }
            if "billing" in url:
                tok = headers.get("Authorization", "")
                if "fresh-token" in tok:
                    return 200, {
                        "config": {
                            "monthlyLimit": {"val": 100},
                            "used": {"val": 10},
                            "billingPeriodStart": "2026-08-01T00:00:00+00:00",
                            "billingPeriodEnd": "2026-09-01T00:00:00+00:00",
                        }
                    }
                return 401, {"error": "expired"}
            if "user" in url:
                return 200, {"email": "m@x.test", "hasGrokCodeAccess": True}
            if "subscriptions" in url:
                return 200, {
                    "subscriptions": [
                        {"status": "SUBSCRIPTION_STATUS_ACTIVE", "tier": "PRO"}
                    ]
                }
            return 404, {}

        self.m._http_json = fake_http
        self.m._grok_cli_nudge = lambda: (_ for _ in ()).throw(AssertionError("cli nudge"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_expired_token_is_refreshed_before_billing(self):
        st = self.m.check_grok()
        self.assertTrue(st.available)
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(st.score, 90.0)
        # First HTTP after ensure_fresh must already be the new bearer.
        billing = [c for c in self.calls if "billing" in c[1]]
        self.assertTrue(billing)
        self.assertIn("fresh-token", billing[0][2])
        self.assertFalse(any(c[0] == "GET" and "401" in str(c) for c in self.calls))
        saved = json.loads(self.auth_path.read_text())
        entry = self.m._grok_pick_entry(saved)
        self.assertEqual(entry["key"], "fresh-token")
        self.assertEqual(entry["refresh_token"], "rt2")

    def test_401_retries_once_then_succeeds(self):
        # Force a still-valid expires_at so ensure_fresh skips, then billing 401s.
        auth = json.loads(self.auth_path.read_text())
        entry = self.m._grok_pick_entry(auth)
        entry["expires_at"] = "2099-01-01T00:00:00Z"
        entry["key"] = "stale-token"
        self.auth_path.write_text(json.dumps(auth))

        st = self.m.check_grok()
        self.assertTrue(st.eligible)
        self.assertAlmostEqual(st.score, 90.0)
        billing = [c for c in self.calls if "billing" in c[1]]
        self.assertGreaterEqual(len(billing), 2)
        self.assertIn("stale-token", billing[0][2])
        self.assertIn("fresh-token", billing[-1][2])


if __name__ == "__main__":
    unittest.main()
