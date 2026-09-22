"""N5.1 — C prefers blue adjudication; Claude only as fallback."""

from __future__ import annotations

import unittest
from unittest import mock

from dossier_engine.authenticator import (
    AuthResult,
    ClaudeAuthenticator,
    map_adjudication_to_auth_result,
)
from dossier_engine.pipeline_health import build_health_payload


class MapAdjudicationTests(unittest.TestCase):
    def test_support_maps_to_authenticated(self):
        out = map_adjudication_to_auth_result(
            {
                "verdict": "support",
                "legacy_verdict": "AUTHENTICATED",
                "path": "nim",
                "note": "NIM support",
                "flags": ["cheap_llm"],
                "reason_codes": ["subject_present"],
            },
            pmid="1",
            claim="c",
            ingredient="ing",
        )
        self.assertTrue(out.passed)
        self.assertEqual(out.claim_supported, "supported")
        self.assertIn("adj_path:nim", out.flags)
        self.assertNotIn("claude", out.flags)

    def test_reject_maps_to_rejected(self):
        out = map_adjudication_to_auth_result(
            {
                "verdict": "reject",
                "legacy_verdict": "REJECTED",
                "path": "claude",
                "note": "hard reject",
                "flags": ["claude_hard_reject"],
                "reason_codes": ["subject_absent"],
            },
            pmid="2",
            claim="c",
            ingredient="ing",
        )
        self.assertFalse(out.passed)
        self.assertEqual(out.verdict_short, "REJECTED")
        self.assertIn("adj_path:claude", out.flags)

    def test_unverified_maps_to_error(self):
        out = map_adjudication_to_auth_result(
            {
                "verdict": "partial",
                "legacy_verdict": "UNVERIFIED",
                "path": "lexical",
                "note": "llm down",
                "flags": ["UNVERIFIED"],
                "reason_codes": [],
            },
            pmid="3",
            claim="c",
            ingredient="ing",
        )
        self.assertEqual(out.verdict_short, "UNVERIFIED")
        self.assertIsNotNone(out.error)


class PreferAdjudicationTests(unittest.TestCase):
    def test_prefers_blue_service_when_reachable(self):
        fake = mock.Mock()
        fake.is_reachable.return_value = True
        fake.adjudicate.return_value = {
            "verdict": "support",
            "legacy_verdict": "AUTHENTICATED",
            "path": "gemini",
            "note": "ok",
            "flags": ["cheap_llm"],
            "reason_codes": [],
        }
        auth = ClaudeAuthenticator(
            api_key="sk-ant-test",
            adjudication=fake,
        )
        # Force preferred path (module flag may already be True).
        with mock.patch(
            "dossier_engine.authenticator.ADJUDICATION_PREFERRED", True
        ):
            out = auth.verify(
                pmid="123",
                claim="helps sleep",
                ingredient="ashwagandha",
                ground_truth={
                    "123": {
                        "title": "t",
                        "abstract": "a",
                        "mesh_terms": [],
                        "article_types": [],
                    }
                },
            )
        self.assertTrue(out.passed)
        self.assertIn("adj_path:gemini", out.flags)
        fake.adjudicate.assert_called_once()

    def test_falls_back_to_claude_when_adj_unreachable(self):
        fake = mock.Mock()
        fake.is_reachable.return_value = False
        auth = ClaudeAuthenticator(
            api_key="",  # explicit empty — do not inherit env key
            adjudication=fake,
        )
        with mock.patch(
            "dossier_engine.authenticator.ADJUDICATION_PREFERRED", True
        ):
            out = auth.verify(
                pmid="123",
                claim="c",
                ingredient="ing",
                ground_truth={"123": {"title": "t", "abstract": "a"}},
            )
        self.assertEqual(out.verdict_short, "UNVERIFIED")
        fake.adjudicate.assert_not_called()

    def test_cost_config_says_volume_path_not_claude(self):
        auth = ClaudeAuthenticator(api_key="sk-ant-test", adjudication=mock.Mock())
        auth._adj_reachable_cache = False
        cfg = auth.describe_cost_config()
        self.assertEqual(cfg["cost_order"][0], "lexical")
        self.assertEqual(cfg["cost_order"][-1], "claude_hard_reject_only")
        self.assertFalse(cfg["default_volume_path_is_claude"])

    def test_health_includes_cost_fields(self):
        payload = build_health_payload(
            auth_available=True,
            model="claude-test",
            cost={
                "cost_order": ["lexical", "nim", "gemini", "claude_hard_reject_only"],
                "adjudication_preferred": True,
                "default_volume_path_is_claude": False,
            },
        )
        self.assertFalse(payload["default_volume_path_is_claude"])
        self.assertEqual(payload["cost_order"][0], "lexical")


if __name__ == "__main__":
    unittest.main()
