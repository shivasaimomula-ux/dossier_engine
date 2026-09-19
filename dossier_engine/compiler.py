"""
compiler.py
===========
Final synthesis. Unlike the original spec's "clean, unhedged" mandate, this
compiler is REQUIRED to:
  - show the evidence tier next to every claim,
  - print explicit "NOT ESTABLISHED / insufficient data" lines for every gap,
  - refuse to render any benefit as substantiated unless it clears threshold,
  - reproduce the regulatory flags verbatim,
  - attach real citations (PubMed DOIs etc.) and nothing else.

The dossier therefore reads as an honest evidence map, with confidence and gaps
as first-class content rather than something to be polished away.
"""

from __future__ import annotations

from .state import GraphState
from .guardrails import EvidenceTier


def _cites_block(rec) -> str:
    if not rec.citations:
        return "_No citation — claim not asserted._"
    return "; ".join(c.render() for c in rec.citations)


def compile_dossier(state: GraphState) -> str:
    f = state.formulation
    out = []
    out.append(f"# PRODUCT EVIDENCE & REGULATORY INFORMATION SHEET")
    out.append(f"**Product:** {f.product_name}  ")
    out.append(f"**Target market:** {f.target_market}  ")
    out.append(f"**Dosage form:** {f.dosage_form or 'not specified'}  ")
    out.append("")
    out.append("> This sheet reports only what queried public sources support. "
               "Fields marked *NOT ESTABLISHED* or *insufficient data* are gaps "
               "the engine could not fill from evidence — they are reported as "
               "gaps, not guesses. This is informational and is not medical "
               "advice, a diagnosis, or a regulatory certification.")
    out.append("")

    # 1. Formulation
    out.append("## 1. Formulation (as stated on label / certificate of analysis)")
    out.append("")
    out.append("| Ingredient | Latin name | Stated dose | Stated standardization | Reference marker (context only) |")
    out.append("|---|---|---|---|---|")
    for ing, _rec in state.label_analysis:
        out.append(
            f"| {ing.name} | {ing.latin_name or '—'} | "
            f"{ing.stated_dose or '**NOT SPECIFIED**'} | "
            f"{ing.stated_standardization or '**NOT SPECIFIED**'} | "
            f"{ing.marker_compound or '—'} |"
        )
    out.append("")
    if state.label_gaps:
        out.append("**Label gaps (no value was assigned to fill these):**")
        for g in state.label_gaps:
            out.append(f"- {g}")
        out.append("")

    # 2. Bioavailability
    out.append("## 2. Bioavailability & dosage-form rationale")
    out.append("")
    for rec in state.bioavailability:
        out.append(f"- **Subject:** {rec.subject}")
        out.append(f"  - **Evidence tier:** {rec.tier.label}")
        out.append(f"  - **Statement:** {rec.safe_statement()}")
        if rec.numeric_value:
            measured = "measured for this product" if rec.numeric_value_is_measured \
                else "NOT measured for this product"
            out.append(f"  - **Quantitative note ({measured}):** {rec.numeric_value}")
        for cav in rec.caveats:
            out.append(f"  - _Caveat:_ {cav}")
        out.append(f"  - **Citations:** {_cites_block(rec)}")
    out.append("")

    # 3. Mechanisms
    out.append("## 3. Mechanistic findings (laboratory / preclinical unless noted)")
    out.append("")
    for rec in state.mechanism_evidence:
        asserted = rec.is_assertable(minimum=EvidenceTier.PRECLINICAL)
        out.append(f"- **{rec.claim}** — tier: {rec.tier.label}")
        if asserted:
            out.append(f"  - {rec.safe_statement()}")
            out.append(f"  - Citations: {_cites_block(rec)}")
        else:
            out.append(f"  - _Not asserted: {rec.safe_statement()}_")
        for cav in rec.caveats:
            out.append(f"  - _Caveat:_ {cav}")
    out.append("")

    # 4. Marketed benefit claims vs evidence
    out.append("## 4. Marketed benefit claims vs. retrieved evidence")
    out.append("")
    out.append("| Claimed benefit | Evidence tier | Status | Citations |")
    out.append("|---|---|---|---|")
    for rec in state.benefit_evidence:
        if rec.tier >= EvidenceTier.PRELIMINARY_HUMAN:
            status = "Some human evidence retrieved"
        elif rec.tier == EvidenceTier.PRECLINICAL:
            status = "Preclinical only — does not support a human claim"
        else:
            status = "**UNSUBSTANTIATED on available evidence**"
        out.append(f"| {rec.claim} | {rec.tier.label} | {status} | {_cites_block(rec)} |")
    out.append("")

    # 5. Traditional use
    out.append("## 5. Traditional use (historical practice — not efficacy)")
    out.append("")
    for rec in state.traditional_use:
        out.append(f"- **{rec.subject}** — {rec.tier.label}")
        for cav in rec.caveats:
            out.append(f"  - {cav}")
    out.append("")

    # 6. Safety & regulatory
    out.append("## 6. Safety, interactions & regulatory flags")
    out.append("")
    if state.safety_flags:
        out.append("**Interaction / contraindication signals:**")
        for s in state.safety_flags:
            out.append(f"- {s}")
        out.append("")
    if state.regulatory_flags:
        out.append("**Regulatory flags (must be resolved before marketing):**")
        for r in state.regulatory_flags:
            out.append(f"- {r}")
        out.append("")

    # Provenance of gaps caused by tool failure
    if state.source_failures:
        out.append("## Appendix: source availability")
        out.append("")
        out.append("Some gaps above are due to a data source being unavailable "
                   "at run time (not a confirmed absence of evidence):")
        for sf in state.source_failures:
            out.append(f"- {sf['source']} — during '{sf['context']}': {sf['error']}")
        out.append("")

    out.append("---")
    out.append("_Citations are drawn only from sources that actually returned "
               "the referenced records. When you cite PubMed-derived findings, "
               "include the DOI links and attribute to PubMed._")
    return "\n".join(out)
