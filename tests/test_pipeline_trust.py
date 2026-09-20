"""Unit tests for PIPELINE_TRUST / UNVERIFIED efficacy cite policy (Task T2)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dossier_engine.pipeline_trust import (  # noqa: E402
    resolve_pipeline_trust,
    should_cite_unverified,
    unverified_auth_decision,
)


class TestResolvePipelineTrust(unittest.TestCase):
    def test_default_and_empty_are_strict(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIPELINE_TRUST", None)
            self.assertEqual(resolve_pipeline_trust(), "strict")
        self.assertEqual(resolve_pipeline_trust(""), "strict")
        self.assertEqual(resolve_pipeline_trust("strict"), "strict")
        self.assertEqual(resolve_pipeline_trust("STRICT"), "strict")
        self.assertEqual(resolve_pipeline_trust("nonsense"), "strict")

    def test_demo_aliases(self):
        for raw in ("demo", "DEMO", "soft", "soft-retain", "soft_retain"):
            self.assertEqual(resolve_pipeline_trust(raw), "demo", msg=raw)

    def test_env_demo(self):
        with patch.dict(os.environ, {"PIPELINE_TRUST": "demo"}):
            self.assertEqual(resolve_pipeline_trust(), "demo")
            self.assertTrue(should_cite_unverified())


class TestUnverifiedAuthDecision(unittest.TestCase):
    def test_strict_omits_unverified(self):
        include, caveat = unverified_auth_decision(
            "12345678", "Authenticator error: timeout", trust_mode="strict"
        )
        self.assertFalse(include)
        self.assertIn("omitted from citations", caveat)
        self.assertIn("PIPELINE_TRUST=strict", caveat)
        self.assertIn("PMID:12345678", caveat)
        self.assertNotIn("paper retained", caveat)

    def test_unset_env_omits_like_strict(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIPELINE_TRUST", None)
            include, caveat = unverified_auth_decision(
                "999", "No Anthropic API key configured"
            )
            self.assertFalse(include)
            self.assertIn("omitted from citations", caveat)

    def test_demo_retains_unverified(self):
        include, caveat = unverified_auth_decision(
            "12345678", "Authenticator error: timeout", trust_mode="demo"
        )
        self.assertTrue(include)
        self.assertIn("paper retained on the MeSH relevance gate", caveat)
        self.assertIn("UNVERIFIED", caveat)
        self.assertNotIn("omitted from citations", caveat)

    def test_env_demo_retains(self):
        with patch.dict(os.environ, {"PIPELINE_TRUST": "demo"}):
            include, caveat = unverified_auth_decision("42", "boom")
            self.assertTrue(include)
            self.assertIn("retained", caveat)

    def test_should_cite_helpers(self):
        self.assertFalse(should_cite_unverified("strict"))
        self.assertTrue(should_cite_unverified("demo"))


if __name__ == "__main__":
    unittest.main()
