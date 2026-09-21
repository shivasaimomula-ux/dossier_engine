"""Unit tests for harden-C health / pipeline readiness contract (Task T1)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dossier_engine.pipeline_health import (  # noqa: E402
    SAFETY_ADJUDICATION_REQUIRED,
    build_health_payload,
    is_pipeline_ready,
)


class TestPipelineHealth(unittest.TestCase):
    def test_always_requires_safety_adjudication(self):
        with_auth = build_health_payload(auth_available=True, model="claude-test")
        without = build_health_payload(auth_available=False, model="claude-test")
        self.assertEqual(with_auth["safety_adjudication"], SAFETY_ADJUDICATION_REQUIRED)
        self.assertEqual(without["safety_adjudication"], "required")
        self.assertEqual(with_auth["build"], "harden-c")

    def test_pipeline_ready_only_when_auth_wired(self):
        ready = build_health_payload(auth_available=True, model="m")
        not_ready = build_health_payload(auth_available=False, model="m")
        self.assertTrue(ready["pipeline_ready"])
        self.assertTrue(ready["auth_available"])
        self.assertFalse(not_ready["pipeline_ready"])
        self.assertFalse(not_ready["auth_available"])

    def test_is_pipeline_ready_rejects_base_semantics(self):
        # Base / pre-harden health bodies lack the adjudication flag.
        base_like = {"ok": True, "auth_available": True, "model": "x"}
        self.assertFalse(is_pipeline_ready(base_like))
        absent = {
            "ok": True,
            "auth_available": True,
            "safety_adjudication": "absent",
            "pipeline_ready": False,
        }
        self.assertFalse(is_pipeline_ready(absent))

    def test_is_pipeline_ready_requires_auth_and_flag(self):
        self.assertTrue(
            is_pipeline_ready(
                build_health_payload(auth_available=True, model="m")
            )
        )
        self.assertFalse(
            is_pipeline_ready(
                build_health_payload(auth_available=False, model="m")
            )
        )

    def test_payload_is_json_serializable(self):
        payload = build_health_payload(auth_available=True, model="claude-sonnet-5")
        encoded = json.dumps(payload)
        self.assertIn('"safety_adjudication": "required"', encoded)


if __name__ == "__main__":
    unittest.main()
