"""Unit tests: C regulatory category resolution (Task T8)."""

from __future__ import annotations

import unittest

from dossier_engine.dossier_emit import build_dossier, enum_resolution_meta
from dossier_engine.state import FormulationInput, GraphState, Ingredient


def _state(*, product_category: str, regulatory_category: str | None = None) -> GraphState:
    return GraphState(
        formulation=FormulationInput(
            product_name="Demo",
            product_category=product_category,
            regulatory_category=regulatory_category,
            target_market="US",
            ingredients=[Ingredient(name="Ashwagandha")],
            claimed_benefits=["support"],
        )
    )


class TestRegulatoryCategoryEmit(unittest.TestCase):
    def test_dietary_supplement_maps(self):
        dossier = build_dossier(_state(product_category="dietary_supplement"), "# md")
        self.assertEqual(str(dossier.regulatory_category), "SUPPLEMENT")
        meta = enum_resolution_meta(dossier)
        self.assertFalse(meta["categoryResolution"]["dropped"])

    def test_explicit_regulatory_wins_over_mesh(self):
        dossier = build_dossier(
            _state(
                product_category="anti-inflammatory",
                regulatory_category="HERBAL",
            ),
            "# md",
        )
        self.assertEqual(str(dossier.regulatory_category), "HERBAL")
        self.assertEqual(dossier.product_category, "anti-inflammatory")

    def test_mesh_indication_drops(self):
        dossier = build_dossier(_state(product_category="laxative"), "# md")
        self.assertIsNone(dossier.regulatory_category)
        self.assertTrue(enum_resolution_meta(dossier)["categoryResolution"]["dropped"])


if __name__ == "__main__":
    unittest.main()
