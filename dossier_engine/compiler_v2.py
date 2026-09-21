"""
compiler_v2.py
==============
Renders the improved architecture's output:
  - Per-ingredient evidence table with relevance status
  - Queried safety findings (not from a static dict)
  - Formulation-level interaction synthesis
  - Audit trail of every claim-evidence link
"""

from __future__ import annotations
import re
from .state import GraphState
from .guardrails import EvidenceTier

# Strip stray XML-ish tags a model may leak into a text field, and neutralize
# characters that would break a markdown table cell.
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _cell(text) -> str:
    """Make an arbitrary string safe for a single markdown table cell."""
    s = _TAG_RE.sub("", str(text))
    return s.replace("\n", " ").replace("|", "\\|").strip()


def _cites(rec) -> str:
    if not rec.citations:
        return "—"
    return "; ".join(c.render() for c in rec.citations)


def compile_v2(state: GraphState) -> str:
    f = state.formulation
    out = []
    out.append("# PRODUCT EVIDENCE & SAFETY INFORMATION SHEET (v2)")
    out.append(f"**Product:** {f.product_name}  ")
    out.append(f"**Category:** {f.product_category or 'not specified'}  ")
    out.append(f"**Target market:** {f.target_market}  ")
    out.append(f"**Dosage form:** {f.dosage_form or 'not specified'}  ")
    if f.serving_size_g:
        out.append(f"**Serving size:** {f.serving_size_g}g  ")
    out.append("")
    out.append("> This sheet reports only what queried public sources support. "
               "Evidence is graded on both study quality AND relevance to the "
               "product's stated purpose. Off-target findings are reported but "
               "NOT counted as support. Gaps are reported, not filled.")
    out.append("")

    # 1. Formulation table (from label dissector if present, else from input)
    out.append("## 1. Formulation")
    out.append("")
    out.append("| Ingredient | Latin name | Amount | Standardization |")
    out.append("|---|---|---|---|")
    for ing in f.ingredients:
        out.append(
            f"| {ing.name} | {ing.latin_name or '—'} | "
            f"{ing.stated_dose or '**NOT SPECIFIED**'} | "
            f"{ing.stated_standardization or '**NOT SPECIFIED**'} |"
        )
    out.append("")

    # 2. Per-ingredient evidence (Priority 1 output)
    out.append("## 2. Per-ingredient evidence for stated purpose "
               f"('{f.product_category or 'general'}')")
    out.append("")
    out.append("| Ingredient | Evidence tier | On-purpose citations | Notes |")
    out.append("|---|---|---|---|")
    for ing, rec in state.per_ingredient_evidence:
        note = (f"{len(rec.caveats)} note(s) — see below"
                if rec.caveats else "—")
        out.append(
            f"| {_cell(ing.name)} | {rec.tier.label} | {_cites(rec)} | {note} |"
        )
    out.append("")

    # Detail any caveats per ingredient (better readability than table)
    for ing, rec in state.per_ingredient_evidence:
        if rec.caveats:
            out.append(f"**{ing.name}:**")
            for c in rec.caveats:
                out.append(f"- {_TAG_RE.sub('', str(c)).strip()}")
            out.append("")

    # 3. Safety per ingredient (Priority 2 output)
    out.append("## 3. Safety & interaction profile per ingredient (QUERIED)")
    out.append("")
    out.append("> Safety searches run independently of efficacy. Safety PMIDs "
               "must pass MeSH/title relevance AND Claude authentication "
               "(same adjudicator as efficacy). Rejected or unverified safety "
               "cites are omitted (fail-closed) — never printed as clearance. "
               "'No data / not cited' = safety NOT assessed, not 'safe'.")
    out.append("")
    for ing, rec in state.queried_safety:
        out.append(f"### {ing.name} ({ing.latin_name or '—'})")
        out.append(f"- **Safety evidence tier:** {rec.tier.label}")
        out.append(f"- **Citations:** {_cites(rec)}")
        for c in rec.caveats:
            out.append(f"- {c}")
        out.append("")

    # 3b. Pharmacology & candidate pathways (PubChem, verified)
    if state.pharmacology:
        out.append("## 3b. Pharmacology & candidate pathways (context)")
        out.append("")
        out.append("> Candidate pathways come from PubChem's curated sources "
                   "(WikiPathways / Reactome / PathBank) for the ISOLATED marker "
                   "compound. Each is verified against PubMed + Claude before "
                   "being marked VERIFIED. Even VERIFIED pathways are mechanistic "
                   "— capped at preclinical, they are NOT evidence of a clinical "
                   "effect in humans and are not specific to this product's dose.")
        out.append("")
        for e in state.pharmacology:
            ident = e.get("identity") or {}
            head = f"### {e['ingredient']}"
            if e.get("compound"):
                head += f" — marker: {e['compound']}"
                if e.get("cid"):
                    head += f" (PubChem CID {e['cid']})"
            out.append(head)
            if ident.get("MolecularFormula"):
                out.append(f"- Identity: {ident.get('MolecularFormula')}, "
                           f"MW {ident.get('MolecularWeight')}")
            if e.get("note"):
                out.append(f"- {e['note']}")
            if e.get("candidates"):
                out.append("")
                out.append("| Candidate pathway | Source | Verdict | Tier | Finding |")
                out.append("|---|---|---|---|---|")
                for c in e["candidates"]:
                    out.append(
                        f"| {_cell(c['name'])} | {_cell(c['source'])} | "
                        f"{c['verdict']} | {c['tier']} | {_cell(c['finding'])} |"
                    )
            out.append("")

    # 4. Formulation-level interaction synthesis
    if state.safety_flags:
        out.append("## 4. Formulation-level interaction synthesis")
        out.append("")
        out.append("> These flags are generated by analyzing how individual "
                   "ingredient safety signals COMPOUND in this specific blend.")
        out.append("")
        for s in state.safety_flags:
            out.append(f"- **{s}**")
        out.append("")

    # 5. Regulatory flags
    if state.regulatory_flags:
        out.append("## 5. Regulatory flags")
        out.append("")
        for r in state.regulatory_flags:
            out.append(f"- {r}")
        out.append("")

    # 6. Claude authentication log
    if state.authentication_log:
        out.append("## 6. Claude Authentication Log")
        out.append("")
        out.append("> Every claim-PMID pair (efficacy, mechanism, and safety) "
                   "was verified by Claude against the real PubMed abstract. "
                   "Papers whose actual findings do not support the attributed "
                   "claim are REJECTED. Safety section is fail-closed: "
                   "UNVERIFIED safety PMIDs are omitted from §3 cites.")
        out.append("")
        passed = [a for a in state.authentication_log if a.passed]
        failed = [a for a in state.authentication_log if not a.passed and not a.error]
        unverified = [a for a in state.authentication_log if a.error]

        if passed:
            out.append("### Authenticated (passed)")
            out.append("")
            out.append("| Ingredient | PMID | Verdict | Evidence level | Finding |")
            out.append("|---|---|---|---|---|")
            for a in passed:
                out.append(
                    f"| {_cell(a.ingredient)} | {a.pmid} | {a.verdict_short} | "
                    f"{_cell(a.evidence_level)} ({_cell(a.species_or_model)}) | "
                    f"{_cell(a.actual_finding)} |"
                )
            out.append("")

        if failed:
            out.append("### Rejected by Claude")
            out.append("")
            out.append("| Ingredient | PMID | Verdict | Reason |")
            out.append("|---|---|---|---|")
            for a in failed:
                flags = ", ".join(a.flags) if a.flags else "—"
                out.append(
                    f"| {_cell(a.ingredient)} | {a.pmid} | **{a.verdict_short}** | "
                    f"{_cell(a.reasoning)} [{_cell(flags)}] |"
                )
            out.append("")

        if unverified:
            out.append("### Unverified (authenticator unavailable)")
            out.append("")
            for a in unverified:
                out.append(f"- {a.ingredient} / PMID:{a.pmid} — {a.error}")
            out.append("")

        # PMID spreading detection
        pmid_counts: dict = {}
        for a in state.authentication_log:
            pmid_counts[a.pmid] = pmid_counts.get(a.pmid, 0) + 1
        spread = {p: c for p, c in pmid_counts.items() if c >= 2}
        if spread:
            out.append("### PMID reuse detection")
            out.append("")
            for pmid, count in sorted(spread.items(), key=lambda x: -x[1]):
                claims = [a.claim for a in state.authentication_log
                          if a.pmid == pmid]
                out.append(
                    f"- **PMID:{pmid}** cited for **{count} claims**: "
                    + "; ".join(f'"{c}"' for c in claims)
                )
            out.append("")

    # 7. Audit trail
    if state.audit_trail:
        out.append("## Appendix A: Claim-evidence link audit trail")
        out.append("")
        out.append("> Every paper retrieved was classified BEFORE grading. "
                   "This trail shows the routing decision for each.")
        out.append("")
        out.append("| Ingredient | PMID | Relevance | Reason |")
        out.append("|---|---|---|---|")
        for link in state.audit_trail:
            out.append(
                f"| {link.ingredient} | {link.pmid} | "
                f"{link.relevance.value} | {link.linkage_reason} |"
            )
        out.append("")

    # Source failures
    if state.source_failures:
        out.append("## Appendix B: Source availability")
        out.append("")
        for sf in state.source_failures:
            out.append(f"- {sf['source']} — {sf['context']}: {sf['error']}")
        out.append("")

    out.append("---")
    out.append("_Evidence retrieved from PubMed. Claims authenticated by Claude "
               f"({state.authentication_log[0].ingredient if state.authentication_log else '—'}). "
               "Cite DOI links and attribute to PubMed when referencing findings._"
               if state.authentication_log else
               "_Evidence retrieved from PubMed. Cite DOI links and attribute "
               "to PubMed when referencing these findings._")
    return "\n".join(out)
