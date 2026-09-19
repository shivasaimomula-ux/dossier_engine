"""
nodes.py
========
The six redesigned agents. Each keeps the *legitimate* job of its counterpart
in the original spec, with the fabrication directives removed:

  Original directive that was removed            ->  What the node does now
  ------------------------------------------------------------------------------
  N2 "extract OR ASSIGN concentration markers"   ->  records only stated specs;
                                                      marks unstated ones as gaps
  N3 "state a quantified bioavailability         ->  reports only figures that
      multiplier"                                     come from a real study, with
                                                      the study's own caveats
  N4 "every pathway MUST have a PMID"             ->  attaches PMIDs only where a
      (forces citation-laundering)                    paper genuinely supports the
                                                      claim; else INSUFFICIENT
  N5 "exact Part/Volume/Page wherever possible"   ->  records traditional use as
                                                      traditional use; never invents
                                                      pharmacopoeia page numbers
  N6 "clean, unhedged" disease-grade dossier      ->  flags claims that exceed the
                                                      evidence or cross into disease
                                                      claims
"""

from __future__ import annotations

from .state import GraphState, Ingredient
from .guardrails import (
    EvidenceRecord, EvidenceTier, grade_pubmed_hits,
)
from .sources import PubMedClient, ChemblClient, ClinicalTrialsClient


# --------------------------------------------------------------------------
# Node 1: Orchestrator
# --------------------------------------------------------------------------
class Orchestrator:
    """Initializes state and runs nodes 2/3/4 (independent), merges, then 5, 6."""

    def __init__(self, pubmed=None, chembl=None, ctgov=None, mcp_call=None):
        self.pubmed = pubmed or PubMedClient(mcp_call=mcp_call)
        self.chembl = chembl or ChemblClient(mcp_call=mcp_call)
        self.ctgov = ctgov or ClinicalTrialsClient(mcp_call=mcp_call)

    def run(self, formulation) -> GraphState:
        state = GraphState(formulation=formulation)
        # "Parallel" tier — independent, could be threaded; order is irrelevant.
        LabelDissector().run(state, self.chembl)
        BioavailabilityNode().run(state, self.pubmed)
        ClinicalVerifier().run(state, self.pubmed)
        # Merge tier
        ScripturalNode().run(state)
        RegulatoryNode().run(state)
        return state


# --------------------------------------------------------------------------
# Node 2: Label & Component Dissector  (Forensic Analytical Chemist)
# --------------------------------------------------------------------------
class LabelDissector:
    def run(self, state: GraphState, chembl: ChemblClient) -> None:
        for ing in state.formulation.ingredients:
            # Record exactly what the label states. Do NOT assign a spec.
            if not ing.stated_standardization:
                state.label_gaps.append(
                    f"{ing.name}: no standardization stated on the label/CoA. "
                    f"Active-marker concentration is NOT ESTABLISHED for this "
                    f"product and must not be assumed."
                )
            if not ing.stated_dose:
                state.label_gaps.append(
                    f"{ing.name}: no per-serving dose stated."
                )

            # Optionally attach a recognized reference marker compound (for
            # context only) — clearly flagged as not a measured product spec.
            res = chembl.lookup(ing.latin_name or ing.name)
            if not res.ok:
                state.note_source_failure("ChEMBL", f"marker for {ing.name}", res.error or "")
            elif res.records:
                top = res.records[0]
                pref = (top.get("pref_name") or top.get("molecule_chembl_id")
                        or "recognized compound")
                ing.marker_compound = str(pref)
                ing.marker_is_measured_for_product = False  # explicit

            rec = EvidenceRecord(
                subject=ing.name,
                claim=f"label composition for {ing.name}",
                tier=EvidenceTier.NONE,
            )
            rec.add_caveat(
                "Composition reflects label/CoA statements only. Where a field "
                "is marked NOT_SPECIFIED, the engine did not have a measured "
                "value and did not assign one."
            )
            state.label_analysis.append((ing, rec))


# --------------------------------------------------------------------------
# Node 3: Bioavailability & Pharmacokinetics  (Pharmacokineticist)
# --------------------------------------------------------------------------
class BioavailabilityNode:
    def run(self, state: GraphState, pubmed: PubMedClient) -> None:
        names = [i.latin_name or i.name for i in state.formulation.ingredients]
        form = state.formulation.dosage_form or "oral"
        query = f"({' OR '.join(names)}) AND bioavailability AND pharmacokinetics"

        res = pubmed.search(query, max_results=8)
        rec = EvidenceRecord(
            subject=", ".join(names),
            claim=f"bioavailability profile of the {form} formulation",
        )
        if not res.ok:
            state.note_source_failure("PubMed", "bioavailability", res.error or "")
            rec.tier = EvidenceTier.NONE
            rec.add_caveat("Literature source was unavailable; gap is due to a "
                           "tool failure, not a confirmed absence of data.")
            state.bioavailability.append(rec)
            return

        tier = grade_pubmed_hits(res.records)
        rec.tier = tier
        for c in PubMedClient.to_citations(res):
            rec.add_citation(c)

        # The classic piperine figure is a teaching case in why NOT to print a
        # clean multiplier: it derives from a small human study where baseline
        # curcumin was near the detection limit. If such a paper is present, we
        # surface the number WITH that caveat rather than as a product spec.
        if any("piperine" in (i.name + (i.latin_name or "")).lower()
               for i in state.formulation.ingredients):
            rec.numeric_value = ("reported increases vary widely across studies; "
                                 "any single percentage is study-specific")
            rec.numeric_value_is_measured = False
            rec.add_caveat(
                "Piperine is reported to inhibit intestinal/hepatic "
                "glucuronidation, increasing curcumin absorption. Reported "
                "magnitudes differ greatly between studies and dosage forms; the "
                "engine does not assign a single 'multiplier' to this product."
            )
        state.bioavailability.append(rec)


# --------------------------------------------------------------------------
# Node 4: Clinical Claim Verifier  (Molecular Pathologist / Researcher)
# --------------------------------------------------------------------------
class ClinicalVerifier:
    # Mechanistic pathways are searched, but a claim is only made if a paper
    # genuinely supports it. No paper -> INSUFFICIENT, never a fabricated PMID.
    MECHANISM_QUERIES = {
        "NF-kB signaling": "NF-kB",
        "COX-2 / inflammation": "cyclooxygenase OR COX-2",
        "STAT3 / MAPK": "STAT3 OR MAPK",
    }

    def run(self, state: GraphState, pubmed: PubMedClient) -> None:
        actives = [i.latin_name or i.name for i in state.formulation.ingredients]
        active_clause = "(" + " OR ".join(actives) + ")"

        # Mechanism mapping (preclinical-leaning by nature)
        for label, mech in self.MECHANISM_QUERIES.items():
            q = f"{active_clause} AND ({mech})"
            res = pubmed.search(q, max_results=6)
            rec = EvidenceRecord(subject=", ".join(actives),
                                 claim=f"modulation of {label}")
            if not res.ok:
                state.note_source_failure("PubMed", label, res.error or "")
                rec.tier = EvidenceTier.NONE
                rec.add_caveat("Source unavailable; not a confirmed absence.")
            elif not res.records:
                rec.tier = EvidenceTier.INSUFFICIENT
                rec.add_caveat("No supporting literature retrieved for this "
                               "pathway claim.")
            else:
                rec.tier = grade_pubmed_hits(res.records)
                for c in PubMedClient.to_citations(res):
                    rec.add_citation(c)
                if rec.tier <= EvidenceTier.PRECLINICAL:
                    rec.add_caveat("Mechanistic / preclinical finding. Does not "
                                   "demonstrate a clinical effect in people.")
            state.mechanism_evidence.append(rec)

        # Marketed benefit claims — the highest-scrutiny check
        for benefit in state.formulation.claimed_benefits:
            q = f"{active_clause} AND ({benefit})"
            res = pubmed.search(q, max_results=8)
            rec = EvidenceRecord(subject=", ".join(actives),
                                 claim=benefit)
            if not res.ok:
                state.note_source_failure("PubMed", benefit, res.error or "")
                rec.tier = EvidenceTier.NONE
                rec.add_caveat("Source unavailable; not a confirmed absence.")
            elif not res.records:
                rec.tier = EvidenceTier.INSUFFICIENT
                rec.add_caveat(f"No literature retrieved supporting the marketed "
                               f"claim '{benefit}'. This claim is UNSUBSTANTIATED "
                               f"on the evidence available to the engine.")
            else:
                rec.tier = grade_pubmed_hits(res.records)
                for c in PubMedClient.to_citations(res):
                    rec.add_citation(c)
            state.benefit_evidence.append(rec)


# --------------------------------------------------------------------------
# Node 5: Scriptural Alignment  (Ayurvedic Scholar)
# --------------------------------------------------------------------------
class ScripturalNode:
    """Records traditional use as TRADITIONAL USE — explicitly not an efficacy
    claim — and never fabricates pharmacopoeia Part/Volume/Page numbers."""

    # A tiny illustrative table of *well-attested* traditional associations.
    # In production, replace with a verified, citable digital API/AP-I index.
    KNOWN_TRADITION = {
        "curcuma longa": "Traditionally described in Ayurvedic texts as having "
                         "'ushna' (heating) virya and used in formulations for "
                         "digestive and inflammatory complaints.",
        "piper nigrum": "Traditionally used as a 'dipana/pachana' (digestive "
                        "stimulant) and as a bioenhancer ('yogavahi').",
    }

    def run(self, state: GraphState) -> None:
        for ing in state.formulation.ingredients:
            key = (ing.latin_name or ing.name).lower()
            rec = EvidenceRecord(subject=ing.name,
                                 claim=f"traditional use of {ing.name}",
                                 tier=EvidenceTier.TRADITIONAL_ONLY)
            if key in self.KNOWN_TRADITION:
                rec.add_caveat(self.KNOWN_TRADITION[key])
            else:
                rec.tier = EvidenceTier.NONE
                rec.add_caveat(
                    "No verified classical reference is available to the engine "
                    "for this ingredient. The engine will not invent a "
                    "pharmacopoeia citation (Part/Volume/Page)."
                )
            rec.add_caveat("Traditional use documents historical practice and is "
                           "NOT evidence of clinical efficacy or safety.")
            state.traditional_use.append(rec)


# --------------------------------------------------------------------------
# Node 6: Global Export & Safety  (Regulatory Auditor)
# --------------------------------------------------------------------------
class RegulatoryNode:
    # Well-established interaction signals worth surfacing for common actives.
    KNOWN_INTERACTIONS = {
        "curcuma longa": [
            ("Anticoagulant / antiplatelet", "May potentiate bleeding risk; "
             "flag for patients on warfarin, aspirin, or clopidogrel."),
            ("Biliary", "May stimulate bile flow; caution / contraindication "
             "advised with gallstones or biliary obstruction."),
        ],
        "piper nigrum": [
            ("CYP/UGT metabolism", "Piperine inhibits certain drug-metabolizing "
             "enzymes; may alter levels of co-administered medications."),
        ],
    }

    def run(self, state: GraphState) -> None:
        # 1) Flag any marketed benefit / mechanism phrased as a disease claim.
        for rec in state.benefit_evidence + state.mechanism_evidence:
            flag = rec.flag_if_disease_claim()
            if flag:
                state.regulatory_flags.append(flag)
            # Flag claims asserted above the evidence available.
            if rec.tier <= EvidenceTier.INSUFFICIENT:
                state.regulatory_flags.append(
                    f"CLAIM WITHOUT SUPPORT: '{rec.claim}' has tier "
                    f"'{rec.tier.label}'. It must not appear as a substantiated "
                    f"benefit on labeling or marketing."
                )

        # 1b) Scan the raw marketed benefits directly. The v2/per-ingredient
        # pipeline does not populate benefit_evidence, so without this a
        # disease claim in claimed_benefits would go unflagged. Skip any
        # benefit already covered above to avoid duplicate flags.
        covered = {rec.claim for rec in state.benefit_evidence}
        for benefit in state.formulation.claimed_benefits:
            if benefit in covered:
                continue
            probe = EvidenceRecord(
                subject=state.formulation.product_name, claim=benefit
            )
            flag = probe.flag_if_disease_claim()
            if flag:
                state.regulatory_flags.append(flag)

        # 2) Known interaction / contraindication surfacing.
        for ing in state.formulation.ingredients:
            key = (ing.latin_name or ing.name).lower()
            for category, text in self.KNOWN_INTERACTIONS.get(key, []):
                state.safety_flags.append(f"[{ing.name}] {category}: {text}")

        # 3) Market frame reminder (not legal advice).
        mkt = state.formulation.target_market.upper()
        frame = "FDA (DSHEA)" if mkt in ("US", "USA") else "EFSA" if mkt in (
            "EU", "EEA") else f"the {mkt} regulator"
        state.regulatory_flags.append(
            f"Target market {mkt}: under {frame}, a food supplement may not make "
            f"disease treatment/prevention claims. Any such phrasing above must "
            f"be removed or the product re-classified."
        )
