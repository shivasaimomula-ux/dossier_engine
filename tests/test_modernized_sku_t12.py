"""Unit tests: Stage C consumes ModernizedSKU (Task T12 / Audit Finding #2)."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from herbenzo_contracts import (
    BCSAssessment,
    BCSClass,
    BioavailabilityEvidence,
    DeliveryRecommendation,
    DeliveryTechnology,
    Dossier,
    MarkerResolution,
    ModernizedIngredient,
    ModernizedSKU,
    PhysicochemicalProfile,
)

from dossier_engine.contract_gate import (
    parse_optional_modernized_sku,
    validate_inbound_formulation,
)
from dossier_engine.dossier_emit import build_dossier, dossier_to_wire
from dossier_engine.guardrails import Citation, EvidenceRecord, EvidenceTier
from dossier_engine.state import FormulationInput, GraphState, Ingredient


def _physchem() -> PhysicochemicalProfile:
    return PhysicochemicalProfile(
        pubchem_cid=969516,
        molecular_weight=368.38,
        xlogp=3.2,
        tpsa=93.1,
        hbd=2,
        hba=6,
        rotatable_bonds=3,
        source="pubchem",
    )


def _sku(**kw) -> ModernizedSKU:
    base = dict(
        sku_id="SKU-TURM-001",
        source_formulation_id="F-TURM-001",
        product_name="TurmaMax Joint Support",
        dosage_form="capsule",
        target_market="US",
        servings_per_day=1,
        ingredients=[
            ModernizedIngredient(
                ingredient_id="HB-TURM",
                botanical_name="Curcuma longa",
                quantity_mg=500.0,
                marker_dose_mg=475.0,
                marker=MarkerResolution(
                    ingredient_id="HB-TURM",
                    marker_name="Curcumin",
                    rationale="primary curcuminoid marker",
                    is_proxy=True,
                    properties=_physchem(),
                ),
                bcs=BCSAssessment(
                    bcs_class=BCSClass.IV,
                    solubility_call="low",
                    permeability_call="low",
                    evidence_basis="computed_descriptors",
                    rationale=["low solubility and permeability descriptors"],
                    confidence=0.72,
                ),
                delivery=DeliveryRecommendation(
                    primary=DeliveryTechnology.PHYTOSOME,
                    alternatives=[DeliveryTechnology.NANOPARTICLE],
                    rationale=["BCS IV — lipid complex may aid absorption"],
                    bioavailability=BioavailabilityEvidence(
                        qualitative_expectation="increase expected, unquantified"
                    ),
                ),
            )
        ],
        confidence=0.75,
        inherited_confidence=0.85,
        engine_version="B-modernizer/1.0.0",
    )
    base.update(kw)
    return ModernizedSKU(**base)


def _state_with_turmeric() -> GraphState:
    formulation = FormulationInput(
        product_name="TurmaMax Joint Support",
        product_category="anti-inflammatory",
        target_market="US",
        dosage_form="capsule",
        serving_size_g=1.0,
        claimed_benefits=["joint comfort"],
        ingredients=[
            Ingredient(
                name="Turmeric extract",
                latin_name="Curcuma longa",
                stated_dose="500 mg",
                stated_standardization="95% curcuminoids",
            )
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
            ),
        )
    ]
    state.queried_safety = []
    state.authentication_log = []
    state.regulatory_flags = []
    state.pharmacology = []
    return state


class TestModernizedSkuAbsent(unittest.TestCase):
    """B-stubbed classical path — modernized_sku null/absent still allowed."""

    def test_gate_allows_absent_sku(self):
        fi = validate_inbound_formulation(
            {
                "product_name": "Classical Blend",
                "ingredients": [{"name": "Ashwagandha"}],
                "modernized_sku": None,
            }
        )
        self.assertIsNone(fi.modernized_sku)
        self.assertIsNone(parse_optional_modernized_sku(fi.modernized_sku))

    def test_dossier_without_sku_omits_bcs_delivery_markers(self):
        state = _state_with_turmeric()
        dossier = build_dossier(state, "# classical\n")
        self.assertFalse(dossier.modernized_sku_present)
        self.assertIsNone(dossier.source_sku_id)
        keys = {s.key for s in dossier.sections}
        self.assertNotIn("bcs", keys)
        self.assertNotIn("delivery", keys)
        self.assertNotIn("markers", keys)
        # Label ingredient left unenriched
        self.assertIsNone(dossier.ingredients[0].ingredient_id)
        self.assertIsNone(dossier.ingredients[0].quantity_mg)
        self.assertIsNone(dossier.ingredients[0].marker_compound)


class TestModernizedSkuPresent(unittest.TestCase):
    """B→C path — validate SKU and compile BCS / delivery / markers."""

    def test_gate_rejects_malformed_sku(self):
        with self.assertRaises(ValidationError):
            validate_inbound_formulation(
                {
                    "product_name": "TurmaMax",
                    "ingredients": [{"name": "Turmeric"}],
                    "modernized_sku": {"sku_id": "only-id"},
                }
            )

    def test_gate_accepts_valid_sku(self):
        sku = _sku()
        fi = validate_inbound_formulation(
            {
                "product_name": "TurmaMax Joint Support",
                "ingredients": [
                    {
                        "name": "Turmeric extract",
                        "latin_name": "Curcuma longa",
                    }
                ],
                "modernized_sku": sku.model_dump(mode="json"),
            }
        )
        parsed = parse_optional_modernized_sku(fi.modernized_sku)
        self.assertIsInstance(parsed, ModernizedSKU)
        self.assertEqual(parsed.sku_id, "SKU-TURM-001")

    def test_dossier_compiles_bcs_delivery_markers(self):
        state = _state_with_turmeric()
        sku = _sku()
        dossier = build_dossier(state, "# with sku\n", modernized_sku=sku)
        self.assertIsInstance(dossier, Dossier)
        self.assertTrue(dossier.modernized_sku_present)
        self.assertEqual(dossier.source_sku_id, "SKU-TURM-001")
        self.assertEqual(dossier.source_formulation_id, "F-TURM-001")
        # Confidence inherited from B (may only lower)
        self.assertEqual(dossier.confidence, 0.75)
        self.assertEqual(dossier.inherited_confidence, 0.85)

        keys = {s.key for s in dossier.sections}
        self.assertIn("markers", keys)
        self.assertIn("bcs", keys)
        self.assertIn("delivery", keys)

        markers = next(s for s in dossier.sections if s.key == "markers")
        self.assertTrue(any("Curcumin" in i.claim for i in markers.items))
        self.assertTrue(any("proxy" in g.lower() for g in markers.gaps))

        bcs = next(s for s in dossier.sections if s.key == "bcs")
        self.assertTrue(any("BCS class IV" in i.claim for i in bcs.items))
        self.assertTrue(
            any("not a regulatory determination" in g for g in bcs.gaps)
        )

        delivery = next(s for s in dossier.sections if s.key == "delivery")
        self.assertTrue(
            any("phytosome_phospholipid_complex" in i.claim for i in delivery.items)
        )
        self.assertTrue(any("advisory_only" in g for g in delivery.gaps))

        # Structured ingredient enrichment from SKU
        ing = dossier.ingredients[0]
        self.assertEqual(ing.ingredient_id, "HB-TURM")
        self.assertEqual(ing.quantity_mg, 500.0)
        self.assertEqual(ing.marker_compound, "Curcumin")

        wire = dossier_to_wire(dossier)
        again = Dossier.model_validate(wire)
        self.assertTrue(again.modernized_sku_present)
        self.assertEqual(again.source_sku_id, "SKU-TURM-001")
        wire_keys = {s["key"] for s in wire["sections"]}
        self.assertEqual({"markers", "bcs", "delivery"} & wire_keys, {"markers", "bcs", "delivery"})


if __name__ == "__main__":
    unittest.main()
