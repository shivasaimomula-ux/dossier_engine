"""
cli.py
======
Run the engine on any formulation described in a JSON file — no code edits.

    python -m dossier_engine --formulation my_product.json
    python -m dossier_engine --formulation my_product.json --no-auth
    python -m dossier_engine --formulation my_product.json --output report.md

The pipeline runs live against PubMed (E-utilities) and, when an
ANTHROPIC_API_KEY is present (env or .env), authenticates every claim-PMID
pair with Claude. Without a key it falls back to the deterministic MeSH gate.

JSON shape (see sample_formulation.json):
{
  "product_name": "...",
  "product_category": "laxative",         # drives the relevance gate
  "target_market": "US",
  "dosage_form": "capsule",
  "serving_size_g": 1.0,
  "claimed_benefits": ["...", "..."],
  "ingredients": [
    {"name": "...", "latin_name": "...",
     "stated_dose": "500 mg", "stated_standardization": "95% ..."}
  ]
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .state import FormulationInput, Ingredient, GraphState
from .nodes import LabelDissector, ScripturalNode, RegulatoryNode
from .nodes_v2 import PerIngredientVerifier, SafetyQueryNode
from .pharmacology import MechanismDiscoveryNode
from .compiler_v2 import compile_v2
from .sources import PubMedClient, PubChemClient
from .authenticator import ClaudeAuthenticator


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def interactive_formulation() -> FormulationInput:
    """Prompt the user for a formulation, field by field, in the terminal."""
    print("\n=== New formulation (press Enter to accept the [default]) ===\n")
    product_name = _ask("Product name", "Unnamed product")
    print("  Common categories: laxative, anti-inflammatory, hepatoprotective, "
          "digestive")
    product_category = _ask("Product category (drives the relevance gate)",
                            "general")
    target_market = _ask("Target market (US / EU / IN)", "US")
    dosage_form = _ask("Dosage form (capsule / powder / syrup / ...)", "")

    print("\n--- Ingredients (enter a blank name when done) ---")
    ingredients = []
    while True:
        name = _ask(f"Ingredient #{len(ingredients) + 1} name")
        if not name:
            break
        latin = _ask("  Latin name", "")
        dose = _ask("  Stated dose (as on label, e.g. '500 mg' or '50% w/w')", "")
        std = _ask("  Stated standardization (e.g. '95% curcuminoids')", "")
        ingredients.append(Ingredient(
            name=name,
            latin_name=latin or None,
            stated_dose=dose or None,
            stated_standardization=std or None,
        ))
    if not ingredients:
        raise ValueError("At least one ingredient is required.")

    print("\n--- Marketed claims (enter a blank line when done) ---")
    claims = []
    while True:
        c = _ask(f"Claim #{len(claims) + 1}")
        if not c:
            break
        claims.append(c)

    return FormulationInput(
        product_name=product_name,
        ingredients=ingredients,
        claimed_benefits=claims,
        target_market=target_market,
        dosage_form=dosage_form or None,
        product_category=product_category,
    )


def load_formulation(path: Path) -> FormulationInput:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_ings = data.get("ingredients", [])
    if not raw_ings:
        raise ValueError("Formulation must list at least one ingredient.")
    ingredients = [
        Ingredient(
            name=i["name"],
            latin_name=i.get("latin_name"),
            stated_dose=i.get("stated_dose"),
            stated_standardization=i.get("stated_standardization"),
        )
        for i in raw_ings
    ]
    return FormulationInput(
        product_name=data.get("product_name", "Unnamed product"),
        ingredients=ingredients,
        claimed_benefits=data.get("claimed_benefits", []),
        target_market=data.get("target_market", "US"),
        dosage_form=data.get("dosage_form"),
        product_category=data.get("product_category", ""),
        serving_size_g=data.get("serving_size_g"),
    )


def run(formulation: FormulationInput, use_auth: bool = True) -> GraphState:
    # Live PubMed with full (MeSH-bearing) hydration so the relevance gate works.
    pubmed = PubMedClient(hydrate_full=True)

    auth = None
    if use_auth:
        candidate = ClaudeAuthenticator(pubmed=pubmed)
        if candidate.available:
            auth = candidate
            print(f"Claude authenticator: ENABLED ({candidate.model})",
                  file=sys.stderr)
        else:
            print("Claude authenticator: DISABLED (no ANTHROPIC_API_KEY) — "
                  "falling back to deterministic MeSH gate", file=sys.stderr)
    else:
        print("Claude authenticator: SKIPPED (--no-auth)", file=sys.stderr)

    state = GraphState(formulation=formulation)
    LabelDissector().run(state, chembl=_NoChembl())
    PerIngredientVerifier(pubmed, authenticator=auth).run(state)
    MechanismDiscoveryNode(PubChemClient(), pubmed, authenticator=auth).run(state)
    SafetyQueryNode(pubmed).run(state)
    ScripturalNode().run(state)
    RegulatoryNode().run(state)
    return state


class _NoChembl:
    """ChEMBL lookup is optional context; skip it on the live CLI path."""
    def lookup(self, name):
        from .sources import SourceResult
        return SourceResult.empty("ChEMBL")


def main(argv=None) -> int:
    import os

    print(
        "WARNING: QUARANTINED base tree — safety §3 is keyword-only. "
        "Use ~/Desktop/dossier_engine-harden-c for pipeline C.",
        file=sys.stderr,
    )
    if os.environ.get("PIPELINE_MODE", "").strip().lower() in ("1", "true", "yes") or \
       os.environ.get("DOSSIER_PIPELINE", "").strip().lower() in ("1", "true", "yes"):
        print(
            "REFUSED: PIPELINE_MODE / DOSSIER_PIPELINE set on quarantined base C. "
            "cd ~/Desktop/dossier_engine-harden-c instead.",
            file=sys.stderr,
        )
        return 2

    parser = argparse.ArgumentParser(
        prog="python -m dossier_engine",
        description="Honest evidence engine — run on a JSON formulation.",
    )
    parser.add_argument("--formulation", "-f", type=Path,
                        help="Path to the formulation JSON file.")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="Enter the formulation interactively at the prompt.")
    parser.add_argument("--no-auth", action="store_true",
                        help="Skip Claude authentication (deterministic only).")
    parser.add_argument("--output", "-o", type=Path,
                        help="Write the report here instead of stdout.")
    args = parser.parse_args(argv)

    if not args.interactive and not args.formulation:
        parser.error("provide --formulation FILE or --interactive")

    try:
        if args.interactive:
            formulation = interactive_formulation()
        else:
            if not args.formulation.exists():
                print(f"Error: formulation file not found: {args.formulation}",
                      file=sys.stderr)
                return 2
            formulation = load_formulation(args.formulation)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"Error reading formulation: {e}", file=sys.stderr)
        return 2

    state = run(formulation, use_auth=not args.no_auth)
    report = compile_v2(state)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
