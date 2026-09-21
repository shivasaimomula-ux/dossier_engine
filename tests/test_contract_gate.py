"""Unit tests: Stage C FormulationInput contract gate (Task T6)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from dossier_engine.contract_gate import validate_inbound_formulation

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_formulation.json"


class TestCContractGate(unittest.TestCase):
    def test_happy_sample(self):
        payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
        fi = validate_inbound_formulation(payload)
        self.assertEqual(fi.product_name, "TurmaMax Joint Support")

    def test_reject_unknown_field(self):
        with self.assertRaises(ValidationError):
            validate_inbound_formulation(
                {
                    "product_name": "X",
                    "ingredients": [{"name": "Y"}],
                    "bogus_key": 1,
                }
            )

    def test_reject_no_ingredients(self):
        with self.assertRaises(ValidationError):
            validate_inbound_formulation({"product_name": "X", "ingredients": []})


if __name__ == "__main__":
    unittest.main()
