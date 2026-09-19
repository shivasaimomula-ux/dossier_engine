"""
run_in_claude.py
================
Runs the engine with CLAUDE AS THE ORCHESTRATOR. No external agent runtime.

How the "in-Claude" execution model works:
  - Claude (the model) issues the real connector calls each node specifies
    (PubMed:search_articles, PubMed:get_article_metadata, ChEMBL:compound_search,
    Clinical Trials:search_trials).
  - Claude drops the returned records into a small in-memory map keyed by the
    query the node will issue.
  - The deterministic guardrail + grading + compiler code (the library) runs
    over those real records. It assigns evidence tiers, refuses weak claims,
    flags disease language, and compiles the dossier.

This file demonstrates it with the records Claude retrieved live for the
turmeric + piperine sample. To run a different product, Claude re-issues the
node queries for that product and refreshes RETRIEVED below.
"""

import json
import os

from dossier_engine import FormulationInput, Ingredient, compile_dossier
from dossier_engine.state import GraphState
from dossier_engine.nodes import (
    LabelDissector, BioavailabilityNode, ClinicalVerifier,
    ScripturalNode, RegulatoryNode,
)
from dossier_engine.sources import PubMedClient, SourceResult


# Records Claude retrieved live from PubMed (loaded from the fixture written
# during this session). In normal in-Claude operation these come straight from
# the connector calls.
with open(os.path.join(os.path.dirname(__file__), "live_records.json")) as fh:
    RETRIEVED = json.load(fh)


class ClaudeBackedPubMed(PubMedClient):
    """A PubMedClient whose results are the records Claude already fetched.
    Maps the node's query intent to the matching retrieved record set; anything
    not retrieved returns an honest empty result (-> INSUFFICIENT/NO DATA)."""

    def search(self, query: str, max_results: int = 8) -> SourceResult:
        q = query.lower()
        if "bioavailability" in q and "pharmacokinetics" in q:
            return SourceResult(ok=True, records=RETRIEVED["bioavailability"], source="PubMed")
        if "arthritis" in q or "joint" in q:
            return SourceResult(ok=True, records=RETRIEVED["arthritis"], source="PubMed")
        # Mechanism / other benefit queries: Claude did not retrieve these in
        # this demo run -> the engine reports them as gaps, honestly.
        return SourceResult.empty("PubMed")


def formulation() -> FormulationInput:
    return FormulationInput(
        product_name="TurmaMax Joint Support",
        dosage_form="capsule",
        target_market="US",
        ingredients=[
            Ingredient("Turmeric extract", "Curcuma longa", "500 mg", "95% curcuminoids"),
            Ingredient("Black pepper extract", "Piper nigrum", "5 mg", None),
        ],
        claimed_benefits=["joint comfort", "cures arthritis", "anti-inflammatory support"],
    )


def main():
    f = formulation()
    pubmed = ClaudeBackedPubMed()
    state = GraphState(formulation=f)

    # Node 1 (Claude) fans out to the specialist nodes:
    LabelDissector().run(state, chembl=_NoChembl())   # marker lookup optional
    BioavailabilityNode().run(state, pubmed)
    ClinicalVerifier().run(state, pubmed)
    ScripturalNode().run(state)
    RegulatoryNode().run(state)

    print(compile_dossier(state))


class _NoChembl:
    """ChEMBL not queried in this demo; returns empty so markers stay '—'
    rather than being invented."""
    def lookup(self, name):
        return SourceResult.empty("ChEMBL")


if __name__ == "__main__":
    main()
