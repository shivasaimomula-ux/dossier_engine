"""
nodes_v2.py
===========
Architectural fixes for the two worst flaws:

  Flaw 1+2+5 (Priority 1): Per-ingredient, relevance-gated retrieval.
    - Each ingredient gets its OWN search (no OR-list conflation).
    - Each paper passes through relevance_filter() BEFORE grading.
    - Off-target papers are logged in audit trail, not counted as support.

  Flaw 4 (Priority 2): Queried safety layer.
    - Safety is a SEARCH, not a static dictionary lookup.
    - Per-ingredient: "{ingredient} AND (adverse effects OR drug interactions
      OR toxicity OR contraindications)".
    - Runs INDEPENDENTLY of efficacy (decoupled), so ingredients with zero
      efficacy data still get safety coverage.
"""

from __future__ import annotations
from typing import Optional, Callable

from .state import GraphState, Ingredient
from .guardrails import (
    EvidenceRecord, EvidenceTier, Citation,
    grade_pubmed_hits, relevance_filter, safety_relevance_filter,
    RelevanceStatus, ClaimEvidenceLink, is_retracted,
)
from .sources import PubMedClient, SourceResult
from .authenticator import ClaudeAuthenticator
from .pipeline_trust import resolve_pipeline_trust, unverified_auth_decision


# --------------------------------------------------------------------------
# Priority 1: Per-ingredient, relevance-gated clinical verifier
# --------------------------------------------------------------------------
class PerIngredientVerifier:
    """
    Per-ingredient, relevance-gated, Claude-authenticated clinical verifier.

    For each ingredient:
      1) Issues a narrow PubMed query: "{latin_name} AND {product_category}"
      2) Checks retraction status
      3) Runs MeSH-based relevance filter (fast, deterministic)
      4) If a ClaudeAuthenticator is provided, sends each on-purpose paper
         through Claude for deep verification — Claude reads the REAL
         abstract and checks whether the paper's actual findings support
         the claim (not just keyword overlap)
      5) Only papers that pass BOTH gates get graded and attached
      6) Off-target, peripheral, and fabricated papers are logged

    UNVERIFIED (transient auth error) policy — ``PIPELINE_TRUST``:
      - ``strict`` (default): omit from compiled citations (align with safety)
      - ``demo``: soft-retain on MeSH gate + UNVERIFIED caveat (legacy)

    The authenticator catches three failure modes that MeSH gating alone
    cannot:
      - Hallucinated PMIDs (PMID doesn't resolve)
      - Single-PMID spreading (one paper cited for many unrelated claims)
      - Evidence-level inflation (mouse study presented as human evidence)
    """

    def __init__(self, pubmed: PubMedClient,
                 authenticator: Optional[ClaudeAuthenticator] = None,
                 trust_mode: Optional[str] = None):
        self.pubmed = pubmed
        self.authenticator = authenticator
        # None → resolve from PIPELINE_TRUST at decision time.
        self.trust_mode = trust_mode

    def run(self, state: GraphState) -> None:
        category = state.formulation.product_category or "general"

        for ing in state.formulation.ingredients:
            name = ing.latin_name or ing.name
            query = f'"{name}" AND {category}'

            res = self.pubmed.search(query, max_results=6)
            rec = EvidenceRecord(
                subject=ing.name,
                claim=f"{ing.name} efficacy for {category} use",
            )

            if not res.ok:
                state.note_source_failure("PubMed", f"efficacy:{ing.name}", res.error or "")
                rec.tier = EvidenceTier.NONE
                rec.add_caveat("Source unavailable; not a confirmed absence.")
                state.per_ingredient_evidence.append((ing, rec))
                continue

            if not res.records:
                rec.tier = EvidenceTier.NONE
                rec.add_caveat(f"No PubMed results for '{query}'. No claim can be made.")
                state.per_ingredient_evidence.append((ing, rec))
                continue

            # ----- Stage 1: retraction + MeSH relevance (deterministic) -----
            on_purpose = []
            off_target = []
            ambiguous = []
            retracted = []

            for paper in res.records:
                if is_retracted(paper):
                    retracted.append(paper)
                    link = ClaimEvidenceLink(
                        claim=rec.claim, ingredient=ing.name,
                        query_issued=query,
                        pmid=str(paper.get("pmid", "")),
                        paper_title=paper.get("title", ""),
                        relevance=RelevanceStatus.RETRACTED,
                        linkage_reason="RETRACTED — excluded from all analysis",
                    )
                    state.audit_trail.append(link)
                    continue

                paper_mesh = set(paper.get("mesh_terms", []))
                paper_title = paper.get("title", "")
                rel = relevance_filter(category, paper_mesh, paper_title)

                link = ClaimEvidenceLink(
                    claim=rec.claim,
                    ingredient=ing.name,
                    query_issued=query,
                    pmid=str(paper.get("pmid", "")),
                    paper_title=paper_title,
                    relevance=rel,
                    linkage_reason=_explain_relevance(rel, category, paper_mesh),
                )
                state.audit_trail.append(link)

                if rel == RelevanceStatus.ON_PURPOSE:
                    on_purpose.append(paper)
                    link.citation = _make_citation(paper)
                elif rel == RelevanceStatus.OFF_TARGET:
                    off_target.append(paper)
                else:
                    ambiguous.append(paper)

            # ----- Stage 2: Claude authentication (if available) -----
            authenticated = on_purpose  # fallback: trust MeSH gate
            if self.authenticator and self.authenticator.available and on_purpose:
                pairs = [
                    {"pmid": str(p.get("pmid", "")),
                     "claim": rec.claim,
                     "ingredient": ing.name}
                    for p in on_purpose
                ]
                auth_results = self.authenticator.verify_batch(
                    pairs, category=category
                )
                state.authentication_log.extend(auth_results)

                authenticated = []
                for paper, auth in zip(on_purpose, auth_results):
                    if auth.passed:
                        authenticated.append(paper)
                        if auth.claim_supported == "partial":
                            rec.add_caveat(
                                f"PMID:{auth.pmid} — Claude: PARTIAL support. "
                                f"{auth.reasoning}"
                            )
                    elif auth.error:
                        # Transient / unavailable auth → UNVERIFIED.
                        # strict (default): omit from cites (fail-closed).
                        # demo: soft-retain on MeSH gate (legacy exploration).
                        mode = self.trust_mode
                        if mode is None:
                            mode = resolve_pipeline_trust()
                        include, caveat = unverified_auth_decision(
                            auth.pmid, auth.error, trust_mode=mode
                        )
                        if include:
                            authenticated.append(paper)
                        rec.add_caveat(caveat)
                    else:
                        detail = auth.reasoning or auth.actual_finding \
                            or "actual findings do not support this claim"
                        rec.add_caveat(
                            f"PMID:{auth.pmid} — Claude {auth.verdict_short}: "
                            f"{detail}"
                        )
                        # Demote to off-target
                        off_target.append(paper)
                        for link in state.audit_trail:
                            if (link.pmid == str(paper.get("pmid", ""))
                                    and link.claim == rec.claim):
                                link.relevance = RelevanceStatus.OFF_TARGET
                                link.linkage_reason = (
                                    f"Claude auth: {auth.verdict_short} — "
                                    f"{auth.actual_finding}"
                                )

            # ----- Stage 3: grade authenticated papers only -----
            if authenticated:
                rec.tier = grade_pubmed_hits(authenticated)
                for p in authenticated[:4]:
                    rec.add_citation(_make_citation(p))
                # If authenticator provided tier overrides, use the lower one
                if self.authenticator and self.authenticator.available:
                    auth_tiers = [
                        ar.authenticated_tier
                        for ar in state.authentication_log
                        if ar.ingredient == ing.name and ar.passed
                        and ar.authenticated_tier > EvidenceTier.NONE
                    ]
                    if auth_tiers:
                        min_auth_tier = min(auth_tiers)
                        if min_auth_tier < rec.tier:
                            rec.add_caveat(
                                f"Evidence tier downgraded from "
                                f"'{rec.tier.label}' to '{min_auth_tier.label}' "
                                f"based on Claude's study-design assessment."
                            )
                            rec.tier = min_auth_tier
            else:
                rec.tier = EvidenceTier.INSUFFICIENT
                if self.authenticator and self.authenticator.available and on_purpose:
                    rec.add_caveat(
                        "Papers were retrieved and passed MeSH relevance, but "
                        "NONE passed Claude authentication — their actual "
                        "findings do not support this product's claims."
                    )
                elif not on_purpose:
                    rec.add_caveat(
                        "Papers were retrieved but NONE passed the "
                        "relevance gate for this product's category."
                    )

            if off_target:
                rec.add_caveat(
                    f"{len(off_target)} paper(s) classified as OFF-TARGET "
                    f"(do not support {category} use): "
                    + ", ".join(f"PMID:{p.get('pmid')}" for p in off_target)
                )
            if ambiguous:
                rec.add_caveat(
                    f"{len(ambiguous)} paper(s) classified as AMBIGUOUS "
                    f"(held for manual review, not counted as support)."
                )
            if retracted:
                rec.add_caveat(
                    f"{len(retracted)} RETRACTED paper(s) excluded: "
                    + ", ".join(f"PMID:{p.get('pmid')}" for p in retracted)
                )

            state.per_ingredient_evidence.append((ing, rec))


# --------------------------------------------------------------------------
# Priority 2: Queried safety layer (replaces static KNOWN_INTERACTIONS dict)
# --------------------------------------------------------------------------
class SafetyQueryNode:
    """
    Runs a dedicated PubMed safety/interaction search per ingredient,
    INDEPENDENTLY of efficacy. An ingredient with zero efficacy data still
    gets full safety coverage.

    Query pattern: "{ingredient}" AND (adverse effects OR drug interactions
                    OR toxicity OR contraindications)

    Trust path (same stack as efficacy, fail-closed for §3 cites):
      1) PubMed keyword search + retraction drop
      2) MeSH/title safety relevance gate (safety_relevance_filter)
      3) ClaudeAuthenticator on on-purpose hits (when available)
      4) Only AUTHENTICATED / PARTIAL (passed) PMIDs become §3 citations

    Fail-closed: rejected OR unverified (no key / transient auth error)
    safety PMIDs are NEVER printed as clean citations. Efficacy matches
    this under PIPELINE_TRUST=strict (default); efficacy soft-retain is
    demo-only. Keyword-only PubMed hits without adjudication do not ship.
    Empty / unverified → honest gap, not clearance.
    """

    def __init__(self, pubmed: PubMedClient,
                 authenticator: Optional[ClaudeAuthenticator] = None):
        self.pubmed = pubmed
        self.authenticator = authenticator

    def run(self, state: GraphState) -> None:
        for ing in state.formulation.ingredients:
            name = ing.latin_name or ing.name
            claim = f"safety/interaction profile of {ing.name}"
            query = (f'"{name}" AND (adverse effects OR drug interactions '
                     f'OR toxicity OR contraindications)')

            res = self.pubmed.search(query, max_results=5)
            rec = EvidenceRecord(subject=ing.name, claim=claim)

            if not res.ok:
                state.note_source_failure("PubMed", f"safety:{ing.name}", res.error or "")
                rec.tier = EvidenceTier.NONE
                rec.add_caveat("Safety source unavailable; safety profile NOT "
                               "assessed — this is a gap, not clearance.")
                state.queried_safety.append((ing, rec))
                continue

            if not res.records:
                rec.tier = EvidenceTier.NONE
                rec.add_caveat(
                    f"No safety/interaction literature retrieved for {ing.name}. "
                    f"Absence of retrieved adverse-effect data is NOT evidence "
                    f"of safety — it means the safety profile was not assessed."
                )
                state.queried_safety.append((ing, rec))
                continue

            # ----- Stage 1: retraction + safety MeSH/title relevance -----
            on_purpose = []
            off_target = []
            ambiguous = []
            retracted_in_safety = []

            for paper in res.records:
                pmid = str(paper.get("pmid", ""))
                paper_title = paper.get("title", "")
                if is_retracted(paper):
                    retracted_in_safety.append(paper)
                    state.audit_trail.append(ClaimEvidenceLink(
                        claim=claim, ingredient=ing.name,
                        query_issued=query, pmid=pmid,
                        paper_title=paper_title,
                        relevance=RelevanceStatus.RETRACTED,
                        linkage_reason="RETRACTED — excluded from safety analysis",
                    ))
                    continue

                paper_mesh = set(paper.get("mesh_terms", []))
                rel = safety_relevance_filter(paper_mesh, paper_title)
                link = ClaimEvidenceLink(
                    claim=claim, ingredient=ing.name,
                    query_issued=query, pmid=pmid,
                    paper_title=paper_title, relevance=rel,
                    linkage_reason=_explain_safety_relevance(rel, paper_mesh),
                )
                state.audit_trail.append(link)

                if rel == RelevanceStatus.ON_PURPOSE:
                    on_purpose.append(paper)
                    link.citation = _make_citation(paper)
                elif rel == RelevanceStatus.OFF_TARGET:
                    off_target.append(paper)
                else:
                    ambiguous.append(paper)

            if retracted_in_safety:
                rec.add_caveat(
                    f"{len(retracted_in_safety)} RETRACTED paper(s) excluded from "
                    f"safety analysis: "
                    + ", ".join(f"PMID:{p.get('pmid')}" for p in retracted_in_safety)
                )
            if off_target:
                rec.add_caveat(
                    f"{len(off_target)} safety-query hit(s) OFF-TARGET "
                    f"(not safety/interaction literature): "
                    + ", ".join(f"PMID:{p.get('pmid')}" for p in off_target)
                )
            if ambiguous:
                rec.add_caveat(
                    f"{len(ambiguous)} safety-query hit(s) AMBIGUOUS "
                    f"(held for manual review, not cited in §3)."
                )

            if not on_purpose:
                rec.tier = EvidenceTier.NONE
                if retracted_in_safety and not (off_target or ambiguous):
                    rec.add_caveat("All retrieved safety papers were retracted.")
                else:
                    rec.add_caveat(
                        "No safety/interaction papers passed the MeSH/title "
                        "relevance gate. Safety profile NOT assessed — gap, "
                        "not clearance."
                    )
                state.queried_safety.append((ing, rec))
                continue

            # ----- Stage 2: Claude authentication (fail-closed for cites) -----
            authenticated = []
            auth_on = bool(self.authenticator and self.authenticator.available)

            if not auth_on:
                # Fail-closed: keyword/MeSH hits without Claude are NOT §3 cites.
                rec.tier = EvidenceTier.NONE
                rec.add_caveat(
                    f"{len(on_purpose)} on-purpose safety paper(s) retrieved "
                    f"but NOT cited — Claude authentication is required for "
                    f"the safety section (fail-closed). Profile NOT cleared. "
                    f"Held PMIDs: "
                    + ", ".join(f"PMID:{p.get('pmid')}" for p in on_purpose[:5])
                )
                # Still harvest MeSH signals for formulation synthesis flags.
                _harvest_safety_mesh(rec, on_purpose)
                state.queried_safety.append((ing, rec))
                continue

            pairs = [
                {"pmid": str(p.get("pmid", "")),
                 "claim": claim,
                 "ingredient": ing.name}
                for p in on_purpose
            ]
            auth_results = self.authenticator.verify_batch(
                pairs, category="safety"
            )
            state.authentication_log.extend(auth_results)

            for paper, auth in zip(on_purpose, auth_results):
                if auth.passed:
                    authenticated.append(paper)
                    if auth.claim_supported == "partial":
                        rec.add_caveat(
                            f"PMID:{auth.pmid} — Claude: PARTIAL safety support. "
                            f"{auth.reasoning}"
                        )
                elif auth.error:
                    # Fail-closed: never soft-retain safety PMIDs.
                    rec.add_caveat(
                        f"PMID:{auth.pmid} — Claude authentication UNVERIFIED "
                        f"({auth.error}); omitted from §3 cites (safety "
                        f"fail-closed). Manual review required."
                    )
                else:
                    detail = auth.reasoning or auth.actual_finding \
                        or "actual findings do not support this safety claim"
                    rec.add_caveat(
                        f"PMID:{auth.pmid} — Claude {auth.verdict_short}: "
                        f"{detail}"
                    )
                    for link in state.audit_trail:
                        if (link.pmid == str(paper.get("pmid", ""))
                                and link.claim == claim):
                            link.relevance = RelevanceStatus.OFF_TARGET
                            link.linkage_reason = (
                                f"Claude auth: {auth.verdict_short} — "
                                f"{auth.actual_finding}"
                            )

            # ----- Stage 3: grade + cite authenticated papers only -----
            if authenticated:
                rec.tier = grade_pubmed_hits(authenticated)
                for p in authenticated[:3]:
                    rec.add_citation(_make_citation(p))
                _harvest_safety_mesh(rec, authenticated)
            else:
                rec.tier = EvidenceTier.NONE
                rec.add_caveat(
                    "On-purpose safety papers were retrieved, but NONE passed "
                    "Claude authentication — rejected/unverified PMIDs are "
                    "omitted from §3. Safety profile NOT cleared."
                )

            state.queried_safety.append((ing, rec))

        # Formulation-level interaction synthesis
        self._synthesize_formulation_interactions(state)

    def _synthesize_formulation_interactions(self, state: GraphState) -> None:
        """Flag ingredient COMBINATIONS whose individual safety signals compound.
        Uses BOTH queried safety MeSH AND pharmacological class knowledge."""
        potassium_losers = []
        sodium_loaders = []
        purgatives = []

        # Pharmacological class knowledge: stimulant laxatives cause K+ loss
        # as a class effect. This won't surface from per-paper MeSH alone.
        STIMULANT_LAXATIVE_GENERA = {"cassia", "senna", "sennosides"}
        MINERALOCORTICOID_RISK = {"glycyrrhiza", "liquorice", "licorice"}

        for ing, rec in state.queried_safety:
            caveats_lower = " ".join(c.lower() for c in rec.caveats)
            latin = (ing.latin_name or "").lower()
            name_lower = ing.name.lower()

            # From queried MeSH
            if any(k in caveats_lower for k in ("hypokalemia", "potassium",
                                                  "electrolyte")):
                potassium_losers.append(ing.name)
            # From pharmacological class
            if any(g in latin or g in name_lower for g in STIMULANT_LAXATIVE_GENERA):
                if ing.name not in potassium_losers:
                    potassium_losers.append(ing.name + " (stimulant-laxative class)")
            if any(g in latin for g in MINERALOCORTICOID_RISK):
                if ing.name not in potassium_losers:
                    potassium_losers.append(ing.name + " (glycyrrhizin effect)")

            # Sodium-bearing preparations
            if any(k in latin for k in ("sauvarchala", "svarjiksara")):
                sodium_loaders.append(ing.name)
            # Licorice causes sodium retention
            if any(g in latin for g in MINERALOCORTICOID_RISK):
                sodium_loaders.append(ing.name + " (sodium retention)")

        # Purgative stacking
        category = (state.formulation.product_category or "").lower()
        if category == "laxative":
            LAXATIVE_GENERA = {"senna", "cassia", "turpethum", "operculina",
                               "terminalia"}
            purgatives = [
                ing.name for ing in state.formulation.ingredients
                if any(k in (ing.latin_name or ing.name).lower()
                       for k in LAXATIVE_GENERA)
            ]

        if len(potassium_losers) >= 2:
            state.safety_flags.append(
                f"COMPOUNDED HYPOKALEMIA RISK: {' + '.join(potassium_losers)} "
                f"each carry potassium-depleting mechanisms. Combined use "
                f"increases the risk; dangerous with digoxin, in cardiac "
                f"patients, or with other potassium-lowering drugs."
            )
        if sodium_loaders:
            state.safety_flags.append(
                f"SODIUM LOAD: {', '.join(sodium_loaders)} are sodium-bearing "
                f"preparations. Combined with any ingredient causing sodium "
                f"retention (e.g. licorice), this is a caution in hypertension / "
                f"cardiac / renal disease."
            )
        if len(purgatives) >= 2:
            state.safety_flags.append(
                f"STACKED PURGATIVE LOAD: {' + '.join(purgatives)} are all "
                f"bowel-active ingredients. Stacking raises the risk of "
                f"cramping, dehydration, and electrolyte disturbance."
            )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _make_citation(paper: dict) -> Citation:
    study_type = None
    for t in paper.get("article_types", []):
        if t.lower() in ("clinical trial", "meta-analysis",
                         "systematic review", "review",
                         "randomized controlled trial"):
            study_type = t
            break
    return Citation(
        source="PubMed",
        identifier=str(paper.get("pmid", "")),
        title=paper.get("title"),
        doi=paper.get("doi"),
        study_type=study_type,
    )


def _harvest_safety_mesh(rec: EvidenceRecord, papers: list) -> None:
    """Attach safety-relevant MeSH signals as caveats (for synthesis flags)."""
    safety_mesh = set()
    for p in papers:
        for m in p.get("mesh_terms", []):
            ml = m.lower()
            if any(kw in ml for kw in (
                "toxicity", "adverse", "hypokalemia", "hypertension",
                "drug interactions", "hepatotoxicity", "nephrotoxicity",
                "contraindications", "poisoning", "overdose",
                "pregnancy", "teratogen", "mutagen",
            )):
                safety_mesh.add(m)
    if safety_mesh:
        rec.add_caveat(
            f"Safety-relevant MeSH signals: {', '.join(sorted(safety_mesh))}"
        )


def _explain_relevance(rel: RelevanceStatus, category: str, mesh: set) -> str:
    if rel == RelevanceStatus.ON_PURPOSE:
        from .guardrails import CATEGORY_MESH
        overlap = CATEGORY_MESH.get(category.lower(), set()) & {m.lower() for m in mesh}
        return f"MeSH overlap on: {', '.join(sorted(overlap))}"
    elif rel == RelevanceStatus.OFF_TARGET:
        from .guardrails import CANCER_INVITRO_MESH
        cancer_overlap = CANCER_INVITRO_MESH & {m.lower() for m in mesh}
        if cancer_overlap:
            return f"Off-target: cancer/in-vitro MeSH ({', '.join(sorted(cancer_overlap))})"
        return "Off-target: no MeSH overlap with product category"
    return "Ambiguous: no category overlap, no clear off-target signal"


def _explain_safety_relevance(rel: RelevanceStatus, mesh: set) -> str:
    if rel == RelevanceStatus.ON_PURPOSE:
        from .guardrails import SAFETY_MESH
        overlap = SAFETY_MESH & {m.lower() for m in mesh}
        if overlap:
            return f"Safety MeSH / signals: {', '.join(sorted(overlap))}"
        return "Safety-relevant title or MeSH substring match"
    elif rel == RelevanceStatus.OFF_TARGET:
        return "Off-target for safety: cancer/in-vitro without safety MeSH"
    return "Ambiguous: no safety MeSH/title signal"
