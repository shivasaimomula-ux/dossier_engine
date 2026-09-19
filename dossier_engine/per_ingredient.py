"""
per_ingredient.py
=================
Per-ingredient evidence pass. Claude (orchestrator) ran a dedicated PubMed
search for each ingredient and read the abstracts; the records below are what
actually came back, classified on TWO axes:

  - tier      : study-strength (via guardrails.grade_pubmed_hits)
  - on_purpose: does the evidence actually bear on THIS product's purpose
                (a laxative/purgative GI use)? Off-target evidence (e.g. an
                in-vitro anticancer assay) is reported as off-target and does
                NOT count toward the product's claim.

Nothing is invented. Ingredients with no usable PubMed record are reported as
explicit gaps.
"""
from dossier_engine.guardrails import EvidenceRecord, EvidenceTier, Citation, grade_pubmed_hits

# Each entry: list of retrieved records (dicts) + per-record relevance + a
# safety note authored from the retrieved safety literature.
INGREDIENTS = [
    {
        "name": "Senna Leaves", "latin": "Cassia angustifolia", "pct": "50%",
        "purpose": "stimulant laxative (the product's main active)",
        "evidence": [
            {"rec": {"pmid":"33767108","doi":"10.14309/ajg.0000000000001222","article_types":["Systematic Review"],"mesh_terms":["Humans","Constipation","Laxatives"]}, "on_purpose": True},
            {"rec": {"pmid":"32969946","doi":"10.14309/ajg.0000000000000942","article_types":["Randomized Controlled Trial"],"mesh_terms":["Humans","Constipation"]}, "on_purpose": True},
            {"rec": {"pmid":"27531591","doi":"10.1002/14651858.CD009118.pub3","article_types":["Meta-Analysis","Systematic Review"],"mesh_terms":["Humans","Constipation"]}, "on_purpose": True},
        ],
        "safety": "Stimulant laxative; grade-A efficacy evidence but NOT for prolonged daily use. Chronic use -> fluid/electrolyte (potassium) loss, dependence. Contraindicated: intestinal obstruction, acute IBD, undiagnosed abdominal pain; caution in pregnancy/children.",
        "safety_cite": ["33767108"],
    },
    {
        "name": "Black Salt", "latin": "Sauvarchala Lavana", "pct": "18%",
        "purpose": "mineral salt / digestive adjunct",
        "evidence": [],  # no usable PubMed record
        "safety": "Predominantly sodium. Adds to the total sodium load (with Svarjiksara) and is a caution in hypertension / cardiac / renal disease. No usable clinical literature retrieved for the named preparation.",
        "safety_cite": [],
    },
    {
        "name": "Ajwain", "latin": "Trachyspermum ammi", "pct": "11.5%",
        "purpose": "carminative / digestive",
        "evidence": [
            {"rec": {"pmid":"35864068","doi":"10.1016/j.jmii.2022.06.006","article_types":["Journal Article"],"mesh_terms":["Humans","Oils, Volatile","Ammi"]}, "on_purpose": False},
        ],
        "safety": "Only off-target preclinical data retrieved (in-vitro anti-parasitic essential-oil study). No retrieved human evidence for a carminative/GI role in this product; treat its contribution as unproven here.",
        "safety_cite": [],
    },
    {
        "name": "Himej (Haritaki)", "latin": "Terminalia chebula", "pct": "8%",
        "purpose": "mild laxative / bowel toner",
        "evidence": [
            {"rec": {"pmid":"31763211","doi":"10.22038/AJP.2019.13109","article_types":["Journal Article"],"mesh_terms":["Humans"]}, "on_purpose": True, "note": "combination product only — effect cannot be attributed to chebula alone"},
            {"rec": {"pmid":"39683707","doi":"10.3390/molecules29235547","article_types":["Review"],"mesh_terms":["Humans","Terminalia","Animals"]}, "on_purpose": False, "note": "broad pharmacology review, not laxative-specific"},
            {"rec": {"pmid":"29288719","doi":"10.1016/j.yrtph.2017.12.019","article_types":["Journal Article"],"mesh_terms":["Animals","Terminalia","Mice"]}, "on_purpose": False, "note": "preclinical toxicity (reassuring)"},
        ],
        "safety": "Preclinical toxicity assessment found the fruit extract relatively non-toxic in acute/subacute animal studies. Laxative effect in humans is documented only within combination formulas; cannot be isolated.",
        "safety_cite": ["29288719"],
    },
    {
        "name": "Svarjiksara", "latin": "Svarjiksara (alkaline ash prep.)", "pct": "5%",
        "purpose": "alkaliser / antacid adjunct",
        "evidence": [],
        "safety": "Alkaline sodium-bearing preparation. Adds to sodium load (with Black Salt). No usable clinical literature retrieved for the named preparation.",
        "safety_cite": [],
    },
    {
        "name": "Yashti Madhu (Licorice)", "latin": "Glycyrrhiza glabra", "pct": "4.5%",
        "purpose": "demulcent / flavour adjunct",
        "evidence": [
            {"rec": {"pmid":"37764649","doi":"10.3390/nu15183866","article_types":["Review"],"mesh_terms":["Glycyrrhiza","Hypokalemia","Hypertension"]}, "on_purpose": False, "note": "safety/toxicity review, not an efficacy claim"},
        ],
        "safety": "KEY SAFETY ITEM. Glycyrrhizin causes pseudohyperaldosteronism -> hypertension, sodium retention, HYPOKALEMIA with sustained intake (104-case review). In THIS product the potassium loss COMPOUNDS with senna's laxative potassium loss, and the sodium retention compounds with Black Salt + Svarjiksara. Caution/contraindication in hypertension, cardiac/renal/hepatic disease, pregnancy.",
        "safety_cite": ["37764649"],
    },
    {
        "name": "Nishoth (Turpeth)", "latin": "Operculina turpethum", "pct": "3%",
        "purpose": "purgative",
        "evidence": [
            {"rec": {"pmid":"28351666","doi":"10.1016/j.archoralbio.2017.03.015","article_types":["Journal Article"],"mesh_terms":["Humans","NF-kappa B","Cell Line, Tumor"]}, "on_purpose": False, "note": "in-vitro oral-cancer assay — not the purgative use"},
            {"rec": {"pmid":"19589336","doi":"10.1016/j.cbi.2009.06.021","article_types":["Journal Article"],"mesh_terms":["Animals","Rats","Liver"]}, "on_purpose": False, "note": "animal hepatoprotective study — not the purgative use"},
        ],
        "safety": "A potent (drastic) purgative by tradition; stacking with senna + Haritaki raises total purgative load and risk of cramping, dehydration, electrolyte disturbance. Retrieved literature is off-target (anticancer in-vitro, animal hepatoprotective); NO human evidence retrieved for its purgative use.",
        "safety_cite": [],
    },
]

def analyze():
    rows = []
    for ing in INGREDIENTS:
        on = [e["rec"] for e in ing["evidence"] if e.get("on_purpose")]
        off = [e for e in ing["evidence"] if not e.get("on_purpose")]
        rec = EvidenceRecord(subject=ing["name"], claim=ing["purpose"])
        if on:
            rec.tier = grade_pubmed_hits(on)
            for r in on:
                rec.add_citation(Citation("PubMed", r["pmid"], doi=r.get("doi"),
                                          study_type=(r["article_types"][0] if r["article_types"] else None)))
        elif off:
            rec.tier = EvidenceTier.INSUFFICIENT
            rec.add_caveat("Only OFF-TARGET evidence retrieved (does not bear on "
                           "this product's purpose); purpose-relevant claim is unproven.")
        else:
            rec.tier = EvidenceTier.NONE
            rec.add_caveat("No usable PubMed record retrieved for this ingredient.")
        rows.append((ing, rec, off))
    return rows

def render():
    rows = analyze()
    out = ["# PER-INGREDIENT EVIDENCE & SAFETY BREAKDOWN",
           "*Ayurvedic Laxative Churna (user-supplied formula) — target market IN*",
           "",
           "> Evidence is graded on study strength AND relevance to the product's "
           "purpose (a laxative). 'Off-target' = real research, but on a different "
           "use; it does NOT support the product's claim. Gaps are reported, not filled.",
           "",
           "## Purpose-relevant evidence per ingredient",
           "",
           "| Ingredient (%) | Stated role | Purpose-relevant tier | Citations (on-purpose) |",
           "|---|---|---|---|"]
    for ing, rec, off in rows:
        cites = "; ".join(c.render() for c in rec.citations) if rec.citations else "—"
        out.append(f"| {ing['name']} ({ing['pct']}) | {ing['purpose']} | {rec.tier.label} | {cites} |")
    out += ["", "## Off-target findings (NOT support for the product's use)", ""]
    any_off = False
    for ing, rec, off in rows:
        for e in off:
            any_off = True
            r = e["rec"]
            note = e.get("note","")
            out.append(f"- **{ing['name']}** — {note}  \n  PubMed:{r['pmid']} https://doi.org/{r['doi']}")
    if not any_off:
        out.append("- None.")
    out += ["", "## Per-ingredient safety / interaction notes", ""]
    for ing, rec, off in rows:
        sc = (" [refs: " + ", ".join(f"PubMed:{p}" for p in ing["safety_cite"]) + "]") if ing["safety_cite"] else ""
        out.append(f"- **{ing['name']} ({ing['latin']})**: {ing['safety']}{sc}")
    out += ["", "## Formulation-level safety synthesis", "",
            "The three compounding mechanisms that matter most in THIS blend:",
            "1. **Potassium depletion stacked:** senna (laxative loss) + licorice "
            "(pseudohyperaldosteronism) → additive hypokalemia risk; dangerous with "
            "digoxin or in cardiac patients.",
            "2. **Sodium load stacked:** Black Salt + Svarjiksara + licorice-driven "
            "sodium retention → caution in hypertension / heart / kidney disease.",
            "3. **Purgative load stacked:** senna + Haritaki + Turpeth → three bowel "
            "actives together raise cramping/dehydration/electrolyte risk.",
            "",
            "**Bottom line:** only senna carries grade-A human efficacy evidence for the "
            "product's purpose. The rest is either unproven for this use or off-target. "
            "The safety interactions — not the efficacy — are the headline. Sustained use "
            "warrants clinician oversight; this sheet is decision-support, not clearance.",
            "",
            "---",
            "_Evidence retrieved from PubMed; cite the DOI links above and attribute to PubMed._"]
    return "\n".join(out)

if __name__ == "__main__":
    print(render())
