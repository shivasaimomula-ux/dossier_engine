"""
run_churna_v2.py
================
Runs the improved architecture (Priority 1 + 2 fixes) on the user's
Ayurvedic laxative churna. Claude (orchestrator) retrieved all records
below from live PubMed searches; the library grades them with the
relevance gate and queried safety layer.

Key improvements over v1:
  - Per-ingredient searches (no OR-list conflation)
  - MeSH-based relevance gate (off-target papers can't upgrade claims)
  - Retracted papers detected and excluded (PMID 34194901)
  - Safety is QUERIED per ingredient, not from a static dictionary
  - Audit trail records every routing decision
"""
from dossier_engine import FormulationInput, Ingredient, ClaudeAuthenticator
from dossier_engine.state import GraphState
from dossier_engine.nodes import LabelDissector, ScripturalNode, RegulatoryNode
from dossier_engine.nodes_v2 import PerIngredientVerifier, SafetyQueryNode
from dossier_engine.compiler_v2 import compile_v2
from dossier_engine.sources import PubMedClient, SourceResult

# ===================================================================
# Real PubMed records retrieved live by Claude, organized per-ingredient
# ===================================================================
EFFICACY = {
    "cassia angustifolia": [
        {"pmid":"33767108","doi":"10.14309/ajg.0000000000001222","title":"Efficacy and Safety of OTC Therapies for Chronic Constipation: An Updated Systematic Review.","article_types":["Systematic Review"],"mesh_terms":["Humans","Constipation","Laxatives","Senna Extract","Nonprescription Drugs"]},
        {"pmid":"32969946","doi":"10.14309/ajg.0000000000000942","title":"Senna Versus Magnesium Oxide for Chronic Constipation: A Randomized, Placebo-Controlled Trial.","article_types":["Randomized Controlled Trial","Comparative Study"],"mesh_terms":["Humans","Constipation","Sennosides","Double-Blind Method","Laxatives"]},
        {"pmid":"27531591","doi":"10.1002/14651858.CD009118.pub3","title":"Osmotic and stimulant laxatives for the management of childhood constipation.","article_types":["Meta-Analysis","Systematic Review"],"mesh_terms":["Humans","Constipation","Sennosides","Senna Extract","Laxatives"]},
    ],
    "trachyspermum ammi": [
        {"pmid":"35864068","doi":"10.1016/j.jmii.2022.06.006","title":"Green synthesis of nano-liposomes containing T. ammi essential oils against Trichomonas vaginalis.","article_types":["Journal Article"],"mesh_terms":["Humans","Oils, Volatile","Ammi","Plant Extracts"]},
    ],
    "terminalia chebula": [
        {"pmid":"31763211","doi":"10.22038/AJP.2019.13109","title":"Effect of an herbal capsule on chronic constipation among menopausal women: a randomized controlled clinical trial.","article_types":["Journal Article"],"mesh_terms":["Humans"]},
        {"pmid":"39683707","doi":"10.3390/molecules29235547","title":"Comprehensive Review on Fruit of Terminalia chebula.","article_types":["Review"],"mesh_terms":["Terminalia","Fruit","Humans","Plant Extracts","Animals","Antioxidants"]},
    ],
    "operculina turpethum": [
        {"pmid":"28351666","doi":"10.1016/j.archoralbio.2017.03.015","title":"Operculina turpethum extract inhibits growth in oral cancer cells.","article_types":["Journal Article"],"mesh_terms":["Antineoplastic Agents, Phytogenic","Apoptosis","Carcinoma, Squamous Cell","Cell Line, Tumor","Convolvulaceae","NF-kappa B","Mouth Neoplasms","Plant Extracts","Tumor Cells, Cultured"]},
        {"pmid":"19589336","doi":"10.1016/j.cbi.2009.06.021","title":"O. turpethum attenuates NDMA induced toxic liver injury in rats.","article_types":["Journal Article"],"mesh_terms":["Animals","Convolvulaceae","Liver","Rats","Plant Extracts"]},
    ],
    "glycyrrhiza glabra": [
        {"pmid":"37764649","doi":"10.3390/nu15183866","title":"Liquorice Toxicity: A Comprehensive Narrative Review.","article_types":["Review"],"mesh_terms":["Glycyrrhiza","Hypokalemia","Hypertension","Candy","Carbonated Beverages"]},
    ],
}

SAFETY = {
    "cassia angustifolia": [
        {"pmid":"33767108","doi":"10.14309/ajg.0000000000001222","title":"Efficacy and Safety of OTC Therapies for Chronic Constipation.","article_types":["Systematic Review"],"mesh_terms":["Humans","Constipation","Laxatives","Senna Extract"]},
        {"pmid":"27531591","doi":"10.1002/14651858.CD009118.pub3","title":"Osmotic and stimulant laxatives for childhood constipation.","article_types":["Meta-Analysis","Systematic Review"],"mesh_terms":["Humans","Constipation","Sennosides","Senna Extract","Laxatives"]},
    ],
    "trachyspermum ammi": [
        {"pmid":"32957888","doi":"10.2174/1570163817999200918103041","title":"Acute and Sub-Acute Toxicity Studies of Trachyspermum ammi.","article_types":["Journal Article"],"mesh_terms":["Ammi","Animals","Inflammation","Rats","Plant Extracts"]},
    ],
    "terminalia chebula": [
        {"pmid":"37007288","doi":"10.3831/KPI.2023.26.1.18","title":"Chronic and Acute Toxicity of Traditional Medicines Containing Terminalia chebula.","article_types":["Journal Article"],"mesh_terms":[]},
        {"pmid":"29288719","doi":"10.1016/j.yrtph.2017.12.019","title":"Safety assessment of Terminalia chebula fruit extract.","article_types":["Journal Article"],"mesh_terms":["Animals","Terminalia","Mice","Mutagenicity Tests","Plant Extracts"]},
    ],
    "operculina turpethum": [
        # 34194901 is RETRACTED — included deliberately to test the filter
        {"pmid":"34194901","doi":"10.1007/s13205-021-02811-x","title":"Ameliorative potential of O. turpethum against streptozotocin-induced diabetes.","article_types":["Journal Article","Retracted Publication"],"mesh_terms":[]},
        {"pmid":"19589336","doi":"10.1016/j.cbi.2009.06.021","title":"O. turpethum attenuates NDMA induced toxic liver injury in rats.","article_types":["Journal Article"],"mesh_terms":["Animals","Rats","Liver","Plant Extracts"]},
    ],
    "glycyrrhiza glabra": [
        {"pmid":"37764649","doi":"10.3390/nu15183866","title":"Liquorice Toxicity: A Comprehensive Narrative Review.","article_types":["Review"],"mesh_terms":["Glycyrrhiza","Hypokalemia","Hypertension"]},
    ],
}


class ClaudePubMedV2(PubMedClient):
    """Routes per-ingredient queries to the correct record set. Because the
    v2 architecture quotes the single ingredient name in each query, routing
    is unambiguous: check which ingredient name the query contains."""

    def __init__(self, efficacy_map, safety_map):
        super().__init__()
        self.efficacy = efficacy_map
        self.safety = safety_map

    def search(self, query, max_results=8):
        q = query.lower()
        # Safety queries contain "adverse effects OR drug interactions"
        is_safety = "adverse effects" in q or "drug interactions" in q or "toxicity" in q

        source = self.safety if is_safety else self.efficacy

        for key, records in source.items():
            if key in q:
                return SourceResult(ok=True, records=records, source="PubMed")

        return SourceResult.empty("PubMed")


class NoChembl:
    def lookup(self, name):
        return SourceResult.empty("ChEMBL")


def formulation():
    return FormulationInput(
        product_name="Ayurvedic Laxative Churna (user-supplied formula)",
        dosage_form="churna (powder)",
        target_market="IN",
        product_category="laxative",
        ingredients=[
            Ingredient("Senna Leaves", "Cassia angustifolia", "50% w/w"),
            Ingredient("Black Salt", "Sauvarchala Lavana", "18% w/w"),
            Ingredient("Ajwain", "Trachyspermum ammi", "11.5% w/w"),
            Ingredient("Himej (Haritaki)", "Terminalia chebula", "8% w/w"),
            Ingredient("Svarjiksara", "Svarjiksara", "5% w/w"),
            Ingredient("Yashti Madhu (Licorice)", "Glycyrrhiza glabra", "4.5% w/w"),
            Ingredient("Nishoth (Turpeth)", "Operculina turpethum", "3% w/w"),
        ],
        claimed_benefits=[
            "relief of occasional constipation "
            "(inferred from formulation; not a user-stated claim)"
        ],
    )


def main():
    f = formulation()
    pubmed = ClaudePubMedV2(EFFICACY, SAFETY)
    state = GraphState(formulation=f)

    # Set up Claude authenticator (uses ANTHROPIC_API_KEY from env).
    # When no key is set, the authenticator is bypassed and the pipeline
    # falls back to the deterministic MeSH-based relevance gate.
    auth = ClaudeAuthenticator(pubmed=pubmed)
    if auth.available:
        print("Claude authenticator: ENABLED", flush=True)
    else:
        print("Claude authenticator: DISABLED (no ANTHROPIC_API_KEY)",
              flush=True)
        auth = None

    # Run the improved pipeline
    LabelDissector().run(state, chembl=NoChembl())
    PerIngredientVerifier(pubmed, authenticator=auth).run(state)
    SafetyQueryNode(pubmed, authenticator=auth).run(state)
    ScripturalNode().run(state)
    RegulatoryNode().run(state)

    print(compile_v2(state))


if __name__ == "__main__":
    main()
