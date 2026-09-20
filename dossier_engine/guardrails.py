"""
guardrails.py
=============
The evidence-grading and refusal layer. This is the architectural core of the
engine: it is what makes the difference between an honest evidence tool and a
fabrication engine.

Design principle (non-negotiable):
    The system NEVER invents a value, a citation, a concentration, or a
    mechanism. When the underlying data source returns nothing — or returns
    only weak evidence — the corresponding field is stamped with an explicit
    "not established / insufficient data" status, and that status propagates
    all the way to the final dossier. Absence of evidence is reported as
    absence of evidence, never silently upgraded into a claim.

Every claim that flows through the graph carries an EvidenceRecord. The final
compiler is forbidden from printing any assertion whose EvidenceRecord does not
meet a minimum tier, and disease-treatment claims are flagged regardless of how
much literature is attached to them (a supplement making a disease-treatment
claim is a regulatory problem independent of the science).
"""

from __future__ import annotations

import enum
import dataclasses
from typing import Optional


class EvidenceTier(enum.IntEnum):
    """
    Ordered strength of evidence. Higher = stronger. The ordering matters:
    the compiler refuses to upgrade language above the tier actually achieved.
    """
    NONE = 0                 # nothing retrieved; do not assert anything
    INSUFFICIENT = 1         # something retrieved but too weak/conflicting to support a claim
    TRADITIONAL_ONLY = 2     # ethnobotanical / historical use only; NOT an efficacy claim
    PRECLINICAL = 3          # in-vitro and/or animal data only
    PRELIMINARY_HUMAN = 4    # small, observational, or single small human trials
    ESTABLISHED_HUMAN = 5    # RCTs / systematic reviews / meta-analyses in humans

    @property
    def label(self) -> str:
        return {
            0: "No data found",
            1: "Insufficient / conflicting data",
            2: "Traditional use only (not an efficacy claim)",
            3: "Preclinical only (in-vitro / animal)",
            4: "Preliminary human evidence",
            5: "Established human evidence",
        }[int(self)]


# Language the engine is permitted to use at each tier. Anything stronger than
# what the tier licenses is a bug. Note that NOTHING below the human tiers is
# allowed to use causal/therapeutic verbs ("treats", "cures", "reduces X in
# patients"). Preclinical findings get hedged, mechanistic phrasing only.
PERMITTED_LANGUAGE = {
    EvidenceTier.NONE: (
        "No data was found in the queried sources. No claim can be made."
    ),
    EvidenceTier.INSUFFICIENT: (
        "Available data is insufficient or conflicting; no reliable claim can be made."
    ),
    EvidenceTier.TRADITIONAL_ONLY: (
        "Traditionally used for this purpose. Traditional use is a record of "
        "historical practice, not evidence of clinical efficacy."
    ),
    EvidenceTier.PRECLINICAL: (
        "Has been studied in laboratory / animal models, where it was reported to "
        "{mechanism}. These are preclinical findings and do not demonstrate an "
        "effect in humans."
    ),
    EvidenceTier.PRELIMINARY_HUMAN: (
        "Early human studies suggest a possible effect ({effect}), but the "
        "evidence is preliminary and not yet confirmed by larger trials."
    ),
    EvidenceTier.ESTABLISHED_HUMAN: (
        "Supported by controlled human trials and/or systematic reviews ({effect})."
    ),
}


# Words that, if used to describe a supplement's effect on a *named disease*,
# convert an information sheet into a (typically non-compliant) drug claim.
# The regulatory node flags these; it does not silently rewrite them.
DISEASE_CLAIM_VERBS = {
    "cure", "cures", "cured", "treat", "treats", "treated", "treatment of",
    "prevent", "prevents", "prevented", "heal", "heals", "reverse", "reverses",
    "eliminates", "remedy for",
}


@dataclasses.dataclass
class Citation:
    """A single piece of provenance. Citations are only ever created from data
    that an upstream tool actually returned — never constructed from memory."""
    source: str                      # e.g. "PubMed", "ChEMBL", "ClinicalTrials.gov"
    identifier: str                  # e.g. PMID, ChEMBL ID, NCT number
    title: Optional[str] = None
    doi: Optional[str] = None
    study_type: Optional[str] = None # e.g. "Clinical Trial", "Review", "in vitro"
    note: Optional[str] = None       # caveats pulled from the record itself

    def render(self) -> str:
        bits = [f"{self.source}:{self.identifier}"]
        if self.study_type:
            bits.append(f"[{self.study_type}]")
        if self.doi:
            bits.append(f"https://doi.org/{self.doi}")
        return " ".join(bits)


@dataclasses.dataclass
class EvidenceRecord:
    """
    Wraps any claim the engine might make. The claim text is the *aspiration*;
    `tier` and `citations` decide whether and how it is allowed to be stated.
    """
    subject: str                     # e.g. "Curcuma longa", "curcumin + piperine"
    claim: str                       # the thing someone wants to assert
    tier: EvidenceTier = EvidenceTier.NONE
    citations: list = dataclasses.field(default_factory=list)
    caveats: list = dataclasses.field(default_factory=list)
    numeric_value: Optional[str] = None      # e.g. a bioavailability figure
    numeric_value_is_measured: bool = False  # True only if it came from an assay/study, never assigned

    def add_citation(self, c: Citation) -> None:
        self.citations.append(c)

    def add_caveat(self, text: str) -> None:
        if text not in self.caveats:
            self.caveats.append(text)

    # ---- guardrail logic -------------------------------------------------

    def is_assertable(self, minimum: EvidenceTier = EvidenceTier.PRECLINICAL) -> bool:
        """A claim may only be stated if it clears the minimum tier AND, for any
        tier that requires evidence, actually carries at least one citation."""
        if self.tier < minimum:
            return False
        if self.tier >= EvidenceTier.PRECLINICAL and not self.citations:
            # We claim to have evidence but have no provenance to back it.
            # That is precisely the failure mode we refuse to produce.
            return False
        return True

    def safe_statement(self) -> str:
        """Render the claim using only language the achieved tier permits."""
        template = PERMITTED_LANGUAGE[self.tier]
        if self.tier in (EvidenceTier.NONE, EvidenceTier.INSUFFICIENT,
                         EvidenceTier.TRADITIONAL_ONLY):
            return template
        # For evidence tiers, splice the claim text into the hedged template.
        try:
            return template.format(mechanism=self.claim, effect=self.claim)
        except (KeyError, IndexError):
            return template

    def flag_if_disease_claim(self) -> Optional[str]:
        """Return a regulatory warning string if the claim reads as a disease
        treatment/prevention claim. Does not modify the claim."""
        lowered = self.claim.lower()
        for verb in DISEASE_CLAIM_VERBS:
            if verb in lowered:
                return (
                    f"REGULATORY FLAG: the phrasing '{self.claim}' reads as a "
                    f"disease treatment/prevention claim. For a food supplement "
                    f"this is generally not a permitted claim under FDA/EFSA "
                    f"frameworks regardless of supporting literature, and would "
                    f"require drug-level authorization."
                )
        return None


# =========================================================================
# Priority 1: MeSH-based relevance gate
# =========================================================================
# Product-category -> set of MeSH terms that count as "on purpose".
# A paper whose MeSH terms overlap this set is on-purpose for that category.
# Expand as you add product types.
CATEGORY_MESH = {
    "laxative": {
        "constipation", "laxatives", "cathartics", "defecation",
        "gastrointestinal transit", "feces", "sennosides", "senna extract",
        "intestinal absorption", "peristalsis", "colon", "purgatives",
        "dietary fiber", "irritable bowel syndrome",
    },
    "anti-inflammatory": {
        "inflammation", "anti-inflammatory agents", "arthritis",
        "osteoarthritis", "nf-kappa b", "cyclooxygenase 2",
        "tumor necrosis factor-alpha", "interleukins", "pain",
    },
    "hepatoprotective": {
        "liver", "hepatitis", "liver cirrhosis", "hepatocytes",
        "chemical and drug induced liver injury", "glutathione",
        "oxidative stress", "cytochrome p-450 enzyme system",
    },
    "digestive": {
        "digestion", "dyspepsia", "flatulence", "colic",
        "gastrointestinal diseases", "stomach", "intestines",
        "appetite", "nausea",
    },
}

# MeSH terms that signal a paper is about an in-vitro cancer assay,
# not a whole-organism therapeutic effect.  These reliably indicate
# off-target evidence for non-oncology products.
CANCER_INVITRO_MESH = {
    "cell line, tumor", "tumor cells, cultured", "antineoplastic agents",
    "antineoplastic agents, phytogenic", "neoplasms", "apoptosis",
    "mouth neoplasms", "carcinoma, squamous cell",
}

# MeSH / title signals that a paper is on-purpose for SAFETY queries
# (adverse effects, interactions, toxicity) — not efficacy.
SAFETY_MESH = {
    "drug-related side effects and adverse reactions",
    "drug interactions", "toxicity", "toxicity tests",
    "chemical and drug induced liver injury", "hepatotoxicity",
    "nephrotoxicity", "poisoning", "overdose", "contraindications",
    "pregnancy", "teratogens", "mutagens", "hypokalemia",
    "hypertension", "adverse effects", "safety",
}


class RelevanceStatus(enum.Enum):
    ON_PURPOSE = "on_purpose"
    OFF_TARGET = "off_target"
    AMBIGUOUS = "ambiguous"
    RETRACTED = "retracted"    # paper was retracted; never count it


def is_retracted(paper: dict) -> bool:
    """Check article_types for retraction notices."""
    types = {t.lower() for t in paper.get("article_types", [])}
    return bool({"retracted publication", "retraction of publication"} & types)


def relevance_filter(
    product_category: str,
    paper_mesh: set,
    paper_title: str = "",
) -> RelevanceStatus:
    """
    Check whether a paper's MeSH terms are relevant to the product's
    therapeutic category.  Returns ON_PURPOSE / OFF_TARGET / AMBIGUOUS.

    This is the fix for flaw #2: study-type grading alone is not enough;
    a brilliant RCT on oral cancer does not support a laxative claim.
    """
    category_terms = CATEGORY_MESH.get(product_category.lower(), set())
    paper_lower = {m.lower() for m in paper_mesh}

    # If the paper is clearly about in-vitro cancer work and the product
    # is not an oncology product, it's off-target.
    if CANCER_INVITRO_MESH & paper_lower and product_category.lower() != "oncology":
        return RelevanceStatus.OFF_TARGET

    # Check overlap with category mesh
    overlap = category_terms & paper_lower
    if overlap:
        return RelevanceStatus.ON_PURPOSE

    # No overlap, no cancer signal — could be tangential or just poorly tagged.
    return RelevanceStatus.AMBIGUOUS


def safety_relevance_filter(
    paper_mesh: set,
    paper_title: str = "",
) -> RelevanceStatus:
    """
    MeSH/title gate for safety/interaction literature.

    A safety PMID must show a safety-relevant MeSH signal (or clear title
    keywords) before Claude authentication. Keyword PubMed hits alone are
    not enough to ship under §3.
    """
    paper_lower = {m.lower() for m in paper_mesh}
    title_lower = (paper_title or "").lower()

    # Pure cancer/in-vitro assays without safety MeSH are off-target for
    # adverse-effect / interaction profiling.
    if CANCER_INVITRO_MESH & paper_lower and not (SAFETY_MESH & paper_lower):
        return RelevanceStatus.OFF_TARGET

    mesh_hit = bool(SAFETY_MESH & paper_lower)
    # Also accept common free-text MeSH fragments / title keywords.
    title_kws = (
        "adverse", "toxicity", "toxic", "interaction", "contraindicat",
        "poison", "overdose", "hepatotox", "nephrotox", "side effect",
        "safety", "teratogen", "pregnancy",
    )
    title_hit = any(k in title_lower for k in title_kws)
    # Substring match on MeSH (PubMed often uses "X / adverse effects")
    mesh_substr = any(
        any(k in m for k in (
            "adverse", "toxicity", "drug interaction", "poisoning",
            "contraindicat", "overdose", "hepatotox", "nephrotox",
            "teratogen", "side effect",
        ))
        for m in paper_lower
    )

    if mesh_hit or mesh_substr or title_hit:
        return RelevanceStatus.ON_PURPOSE
    return RelevanceStatus.AMBIGUOUS


@dataclasses.dataclass
class ClaimEvidenceLink:
    """
    Auditable record of WHY a citation was attached to a claim.
    Fixes flaw #1: makes the retrieval-to-claim attachment reviewable.
    """
    claim: str
    ingredient: str
    query_issued: str
    pmid: str
    paper_title: str
    relevance: RelevanceStatus
    linkage_reason: str          # human-readable: "MeSH overlap on {terms}"
    citation: Optional[Citation] = None

    def render_audit(self) -> str:
        return (
            f"  PMID {self.pmid} -> claim '{self.claim}' | "
            f"relevance={self.relevance.value} | {self.linkage_reason}"
        )


def grade_pubmed_hits(hits: list) -> EvidenceTier:
    """
    Map a list of retrieved PubMed records to an evidence tier based on the
    *types* of studies found. Conservative by construction: a single review
    does not become 'established human'; only controlled human trials /
    meta-analyses reach the top tier.

    `hits` is a list of dicts each optionally containing 'article_types' and
    'mesh_terms' (as returned by the metadata tool).

    RETRACTED papers are excluded entirely.
    """
    if not hits:
        return EvidenceTier.NONE

    has_human_trial = False
    has_meta = False
    has_any_human = False
    has_animal_or_invitro = False

    for h in hits:
        types = {t.lower() for t in h.get("article_types", [])}
        mesh = {m.lower() for m in h.get("mesh_terms", [])}

        # Skip retracted papers — they should not contribute to any tier.
        if "retracted publication" in types or "retraction of publication" in types:
            continue

        if {"meta-analysis", "systematic review"} & types:
            has_meta = True
        if "clinical trial" in types or "randomized controlled trial" in types:
            has_human_trial = True
        if "humans" in mesh:
            has_any_human = True
        if {"animals", "rats", "mice", "in vitro techniques"} & mesh:
            has_animal_or_invitro = True

    if has_meta or has_human_trial:
        return EvidenceTier.ESTABLISHED_HUMAN
    if has_any_human:
        return EvidenceTier.PRELIMINARY_HUMAN
    if has_animal_or_invitro:
        return EvidenceTier.PRECLINICAL
    # Retrieved something, but can't classify it as supporting a claim.
    return EvidenceTier.INSUFFICIENT
