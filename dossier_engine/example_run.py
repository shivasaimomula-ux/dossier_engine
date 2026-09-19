"""
example_run.py
==============
Runs the engine on a sample turmeric + black pepper formulation.

Two modes:
  - Offline (default here): no network. Demonstrates that every evidence-backed
    field degrades to an explicit "NOT ESTABLISHED / source unavailable" gap
    rather than a fabricated value. This is the guardrail proof.
  - Live: pass real clients (PubMedClient over E-utilities, or an mcp_call
    bridge) to the Orchestrator to populate citations from real records.

Run:  python -m dossier_engine.example_run
"""

from dossier_engine import (
    FormulationInput, Ingredient, Orchestrator, compile_dossier,
)


def sample_formulation() -> FormulationInput:
    return FormulationInput(
        product_name="TurmaMax Joint Support",
        dosage_form="capsule",
        target_market="US",
        ingredients=[
            Ingredient(
                name="Turmeric extract",
                latin_name="Curcuma longa",
                stated_dose="500 mg",
                stated_standardization="95% curcuminoids",  # stated on label
            ),
            Ingredient(
                name="Black pepper extract",
                latin_name="Piper nigrum",
                stated_dose="5 mg",
                stated_standardization=None,  # label is silent -> becomes a GAP
            ),
        ],
        claimed_benefits=[
            "joint comfort",
            "cures arthritis",          # deliberately non-compliant -> flagged
            "anti-inflammatory support",
        ],
    )


def main():
    formulation = sample_formulation()
    # Offline: no clients passed -> source calls fail -> honest gaps.
    orch = Orchestrator()
    state = orch.run(formulation)
    print(compile_dossier(state))


if __name__ == "__main__":
    main()
