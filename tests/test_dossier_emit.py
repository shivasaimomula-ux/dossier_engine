"""Unit tests: Stage C structured Dossier emit (Task T7)."""

from __future__ import annotations

import unittest

from herbenzo_contracts import Dossier

from dossier_engine.authenticator import AuthResult
from dossier_engine.dossier_emit import build_dossier, dossier_to_wire
from dossier_engine.guardrails import Citation, EvidenceRecord, EvidenceTier
from dossier_engine.state import FormulationInput, GraphState, Ingredient


def _minimal_state() -> GraphState:
    formulation = FormulationInput(
        product_name="TurmaMax Joint Support",
        product_category="anti-inflammatory",
        target_market="US",
        dosage_form="capsule",
        serving_size_g=1.0,
        claimed_benefits=["joint comfort", "cures arthritis"],
        ingredients=[
            Ingredient(
                name="Turmeric extract",
                latin_name="Curcuma longa",
                stated_dose="500 mg",
                stated_standardization="95% curcuminoids",
            ),
            Ingredient(
                name="Black pepper extract",
                latin_name="Piper nigrum",
                stated_dose="5 mg",
            ),
        ],
    )
    state = GraphState(formulation=formulation)
    turmeric = formulation.ingredients[0]
    state.per_ingredient_evidence = [
        (
            turmeric,
            EvidenceRecord(
                subject="Turmeric extract",
                claim="anti-inflammatory support",
                tier=EvidenceTier.PRELIMINARY_HUMAN,
                citations=[
                    Citation(
                        source="PubMed",
                        identifier="12345678",
                        study_type="Clinical Trial",
                    )
                ],
                caveats=["small sample"],
            ),
        )
    ]
    state.queried_safety = [
        (
            turmeric,
            EvidenceRecord(
                subject="Turmeric extract",
                claim="generally recognized as safe at stated dose",
                tier=EvidenceTier.ESTABLISHED_HUMAN,
                citations=[
                    Citation(source="PubMed", identifier="87654321")
                ],
            ),
        )
    ]
    state.authentication_log = [
        AuthResult(
            pmid="12345678",
            claim="anti-inflammatory support",
            ingredient="Turmeric extract",
            pmid_valid=True,
            claim_supported="supported",
            evidence_level="human_pilot",
            finding_type="primary",
            actual_finding="reduced CRP",
            reasoning="Abstract supports claim",
        ),
        AuthResult(
            pmid="99999999",
            claim="cures arthritis",
            ingredient="Turmeric extract",
            error="timeout",
            flags=["AUTH_ERROR"],
        ),
    ]
    state.regulatory_flags = [
        "REGULATORY FLAG: 'cures arthritis' reads as a disease claim"
    ]
    state.safety_flags = []
    state.pharmacology = [
        {
            "ingredient": "Turmeric extract",
            "candidates": [
                {
                    "name": "NF-kB",
                    "verdict": "VERIFIED",
                    "tier": "preclinical",
                    "finding": "inhibits NF-kB in vitro",
                    "citation": "PMID:11112222",
                }
            ],
        }
    ]
    return state


class TestDossierEmit(unittest.TestCase):
    def test_emits_versioned_dossier_alongside_markdown(self):
        state = _minimal_state()
        md = "# TurmaMax\n\nHuman-readable companion.\n"
        dossier = build_dossier(state, md, confidence_floor=0.8)
        self.assertIsInstance(dossier, Dossier)
        self.assertEqual(dossier.schema_version, "1.0.0")
        self.assertEqual(dossier.product_name, "TurmaMax Joint Support")
        self.assertEqual(dossier.markdown, md.rstrip())
        self.assertGreaterEqual(len(dossier.ingredients), 2)
        self.assertEqual(dossier.confidence, 0.8)
        self.assertEqual(dossier.inherited_confidence, 0.8)
        self.assertFalse(dossier.modernized_sku_present)

        keys = {s.key for s in dossier.sections}
        self.assertIn("per_ingredient_efficacy", keys)
        self.assertIn("safety", keys)
        self.assertIn("authentication", keys)
        self.assertIn("pharmacology", keys)
        self.assertIn("regulatory", keys)

        auth = next(s for s in dossier.sections if s.key == "authentication")
        statuses = {i.status for i in auth.items}
        self.assertIn("supported", statuses)
        self.assertIn("unverified", statuses)
        self.assertTrue(any("UNVERIFIED" in g for g in auth.gaps))

        wire = dossier_to_wire(dossier)
        self.assertEqual(wire["schema_version"], "1.0.0")
        self.assertIn("dossier_id", wire)
        self.assertIn("ingredients", wire)
        # Round-trip through shared contract
        again = Dossier.model_validate(wire)
        self.assertEqual(again.dossier_id, dossier.dossier_id)

    def test_confidence_may_only_lower(self):
        state = _minimal_state()
        dossier = build_dossier(
            state,
            "# md",
            confidence=0.5,
            inherited_confidence=0.9,
        )
        self.assertEqual(dossier.confidence, 0.5)
        self.assertEqual(dossier.inherited_confidence, 0.9)

    def test_rejects_empty_ingredients(self):
        state = GraphState(
            formulation=FormulationInput(
                product_name="Empty",
                ingredients=[],
                claimed_benefits=[],
            )
        )
        with self.assertRaises(ValueError):
            build_dossier(state, "# md")


if __name__ == "__main__":
    unittest.main()
