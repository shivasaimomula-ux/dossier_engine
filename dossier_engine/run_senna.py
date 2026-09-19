"""
run_senna.py — in-Claude run on the user's real laxative formulation.
Claude (orchestrator) retrieved the records below live from PubMed; the
deterministic library grades and compiles them.
"""
from dossier_engine import FormulationInput, Ingredient, compile_dossier
from dossier_engine.state import GraphState
from dossier_engine.nodes import (
    LabelDissector, BioavailabilityNode, ClinicalVerifier,
    ScripturalNode, RegulatoryNode,
)
from dossier_engine.sources import PubMedClient, SourceResult

# --- Records Claude retrieved live (PubMed) ---
LAXATIVE_EVIDENCE = [
    {"pmid":"33767108","doi":"10.14309/ajg.0000000000001222","title":"Efficacy and Safety of OTC Therapies for Chronic Constipation: An Updated Systematic Review.","article_types":["Journal Article","Systematic Review"],"mesh_terms":["Humans","Constipation","Laxatives","Senna Extract"]},
    {"pmid":"32969946","doi":"10.14309/ajg.0000000000000942","title":"Senna Versus Magnesium Oxide for the Treatment of Chronic Constipation: A Randomized, Placebo-Controlled Trial.","article_types":["Randomized Controlled Trial","Comparative Study"],"mesh_terms":["Humans","Constipation","Sennosides","Double-Blind Method"]},
    {"pmid":"27531591","doi":"10.1002/14651858.CD009118.pub3","title":"Osmotic and stimulant laxatives for the management of childhood constipation.","article_types":["Meta-Analysis","Systematic Review"],"mesh_terms":["Humans","Constipation","Sennosides","Laxatives"]},
]
CHEBULA_EVIDENCE = [
    {"pmid":"31763211","doi":"10.22038/AJP.2019.13109","title":"Effect of an herbal capsule (incl. Terminalia chebula, senna) on chronic constipation among menopausal women: an RCT.","article_types":["Journal Article"],"mesh_terms":["Humans"]},
]
LICORICE_SAFETY = [
    {"pmid":"37764649","doi":"10.3390/nu15183866","title":"Liquorice Toxicity: A Comprehensive Narrative Review.","article_types":["Journal Article","Review"],"mesh_terms":["Glycyrrhiza","Hypokalemia","Hypertension"]},
]

class ClaudePubMed(PubMedClient):
    """Routes on the DISTINCTIVE part of each node's query, not the shared
    ingredient list. Anything Claude did not actually retrieve returns empty,
    which the engine reports as an honest gap."""
    def search(self, query, max_results=8):
        q = query.lower()
        # Bioavailability node: I did not retrieve PK data for this formula.
        if "bioavailability" in q and "pharmacokinetics" in q:
            return SourceResult.empty("PubMed")
        # Mechanism node pathways (NF-kB / COX / STAT3): not relevant to a
        # laxative and not retrieved -> honest gap.
        if any(k in q for k in ("nf-kb", "cyclooxygenase", "cox-2", "stat3", "mapk")):
            return SourceResult.empty("PubMed")
        # The intended-use claim: constipation relief -> strong senna evidence.
        if "constipat" in q:
            return SourceResult(ok=True, records=LAXATIVE_EVIDENCE, source="PubMed")
        return SourceResult.empty("PubMed")

class NoChembl:
    def lookup(self, name): return SourceResult.empty("ChEMBL")

# Extend the safety knowledge for THIS formulation's real, documented signals.
RegulatoryNode.KNOWN_INTERACTIONS.update({
    "cassia angustifolia": [
        ("Stimulant-laxative class", "Senna is a stimulant laxative. Not intended for prolonged daily use; chronic use is associated with fluid/electrolyte loss (esp. potassium) and laxative dependence. Contraindicated in intestinal obstruction, acute inflammatory bowel conditions, undiagnosed abdominal pain; caution in pregnancy and children."),
        ("Compounded hypokalemia", "Potassium loss from stimulant laxation can ADD to the potassium-lowering effect of licorice (Glycyrrhiza) in the same product, and can potentiate digoxin toxicity and the effect of other drugs affected by low potassium."),
    ],
    "glycyrrhiza glabra": [
        ("Pseudohyperaldosteronism", "Glycyrrhizin can cause hypertension, sodium retention, and hypokalemia with sustained intake. Risk compounds with the sodium load from Black Salt + Svarjiksara and the potassium loss from senna in this formulation. Caution/contraindication in hypertension, cardiac, renal, or hepatic disease, and pregnancy."),
    ],
    "operculina turpethum": [
        ("Drastic purgative", "Turpeth (Nishoth) is a potent purgative; stacking it with senna and Terminalia chebula increases the total purgative load and the risk of cramping, dehydration, and electrolyte disturbance."),
    ],
})

def main():
    f = FormulationInput(
        product_name="Ayurvedic Laxative Churna (user-supplied formula)",
        dosage_form="churna (powder)",
        target_market="IN",
        ingredients=[
            Ingredient("Senna Leaves","Cassia angustifolia",stated_dose="50% w/w",stated_standardization=None),
            Ingredient("Black Salt","Sauvarchala Lavana",stated_dose="18% w/w",stated_standardization=None),
            Ingredient("Ajwain","Trachyspermum ammi",stated_dose="11.5% w/w",stated_standardization=None),
            Ingredient("Himej (Haritaki)","Terminalia chebula",stated_dose="8% w/w",stated_standardization=None),
            Ingredient("Svarjiksara","Svarjiksara",stated_dose="5% w/w",stated_standardization=None),
            Ingredient("Yashti Madhu (Licorice)","Glycyrrhiza glabra",stated_dose="4.5% w/w",stated_standardization=None),
            Ingredient("Nishoth (Turpeth)","Operculina turpethum",stated_dose="3% w/w",stated_standardization=None),
        ],
        # No marketing claims were provided by the user. We do NOT invent any.
        # We verify the formulation's evident intended use against the literature
        # and clearly mark it as an inferred indication, not a stated claim.
        claimed_benefits=["relief of occasional constipation (inferred from formulation; not a user-stated claim)"],
    )
    pubmed = ClaudePubMed()
    state = GraphState(formulation=f)
    LabelDissector().run(state, chembl=NoChembl())
    BioavailabilityNode().run(state, pubmed)
    ClinicalVerifier().run(state, pubmed)
    ScripturalNode().run(state)
    RegulatoryNode().run(state)
    print(compile_dossier(state))

if __name__ == "__main__":
    main()
