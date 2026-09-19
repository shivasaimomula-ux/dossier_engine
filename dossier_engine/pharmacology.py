"""
pharmacology.py
===============
Mechanism discovery via PubChem, gated by the same skepticism as everything
else in the engine.

Flow per ingredient:
  1. Resolve the ingredient's marker COMPOUND to a PubChem CID (botanical names
     don't resolve; we map to the active or read it from the standardization).
  2. Pull the curated pathways PubChem associates with that compound
     (WikiPathways / Reactome / PathBank / ...). These are CANDIDATE mechanisms.
  3. For each distinct candidate, search PubMed for the compound + pathway and
     run the papers through the SAME Claude authenticator used for efficacy.
       - Authenticated  -> asserted as a mechanistic finding, capped at the
         PRECLINICAL tier and carrying the mandatory "not a human clinical
         effect" caveat.
       - Not supported  -> reported as a database-reported association for the
         ISOLATED compound only: mechanistic context, never efficacy, and it
         can never raise a tier.

Nothing is invented: a pathway with no supporting literature is shown as an
unverified database association, and an ingredient whose compound doesn't
resolve is an explicit gap.
"""

from __future__ import annotations

import re
from typing import Optional

from .state import GraphState
from .guardrails import EvidenceRecord, EvidenceTier
from .sources import PubChemClient, PubMedClient
from .authenticator import ClaudeAuthenticator


# Small, illustrative botanical -> principal marker-compound seed map. Replace
# with a curated phytochemical index for production. Only used when the
# ingredient does not already name a resolvable compound.
BOTANICAL_MARKERS = {
    "curcuma longa": "curcumin",
    "piper nigrum": "piperine",
    "cassia angustifolia": "sennoside A",
    "senna": "sennoside A",
    "glycyrrhiza glabra": "glycyrrhizin",
    "withania somnifera": "withaferin A",
    "terminalia chebula": "chebulinic acid",
    "trachyspermum ammi": "thymol",
    "zingiber officinale": "6-gingerol",
    "bacopa monnieri": "bacoside A",
    "ocimum sanctum": "eugenol",
    "boswellia serrata": "boswellic acid",
}

# Words that carry no discriminating signal in a pathway name — dropped when
# building the PubMed verification query.
_STOP = {
    "transcriptional", "activation", "response", "phytochemicals", "pathway",
    "pathways", "signaling", "signalling", "in", "to", "by", "of", "the", "and",
    "regulation", "metabolism", "process", "via", "a", "an", "degradation",
}

# Marker compounds a standardization string may name (e.g. "95% curcuminoids").
_STD_MARKERS = [
    ("curcumin", "curcumin"), ("piperine", "piperine"),
    ("sennoside", "sennoside A"), ("withanolide", "withaferin A"),
    ("glycyrrhizin", "glycyrrhizin"), ("gingerol", "6-gingerol"),
    ("boswellic", "boswellic acid"), ("thymol", "thymol"),
]


def _marker_from_standardization(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    for needle, compound in _STD_MARKERS:
        if needle in t:
            return compound
    return None


def _candidate_names(ing) -> list:
    """Ordered list of compound names to try resolving against PubChem."""
    names = []
    if getattr(ing, "marker_compound", None):
        names.append(ing.marker_compound)
    std_marker = _marker_from_standardization(ing.stated_standardization)
    if std_marker:
        names.append(std_marker)
    latin = (ing.latin_name or "").lower()
    if latin in BOTANICAL_MARKERS:
        names.append(BOTANICAL_MARKERS[latin])
    nm = ing.name.lower()
    if nm in BOTANICAL_MARKERS:
        names.append(BOTANICAL_MARKERS[nm])
    # Last resort: the raw names (works when the ingredient IS a compound).
    names.append(ing.name)
    if ing.latin_name:
        names.append(ing.latin_name)
    # de-dupe, preserve order
    seen, out = set(), []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def _dedupe_pathways(records: list) -> list:
    """Collapse near-duplicate pathway names to distinct mechanisms."""
    seen, out = set(), []
    for r in records:
        key = re.sub(r"[^a-z0-9]", "", r["name"].lower())
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _query_terms(pathway_name: str) -> str:
    """Build a PubMed clause from the discriminating tokens of a pathway name.
    Keeps gene/entity-like tokens (NRF2, NFE2L2, COX2) and content words."""
    tokens = re.findall(r"[A-Za-z0-9\-]+", pathway_name)
    entities, words = [], []
    for tok in tokens:
        low = tok.lower()
        if low in _STOP or len(tok) < 2:
            continue
        if re.search(r"[A-Z]", tok) and re.search(r"[A-Z0-9]", tok[1:]):
            entities.append(tok)          # gene/protein-like
        else:
            words.append(tok)
    picked = entities[:2] or words[:2]
    if not picked:
        return ""
    return "(" + " OR ".join(dict.fromkeys(picked)) + ")"


class MechanismDiscoveryNode:
    def __init__(self, pubchem: PubChemClient, pubmed: PubMedClient,
                 authenticator: Optional[ClaudeAuthenticator] = None,
                 max_pathways: int = 4):
        self.pubchem = pubchem
        self.pubmed = pubmed
        self.authenticator = authenticator
        self.max_pathways = max_pathways

    def run(self, state: GraphState) -> None:
        category = state.formulation.product_category or "general"
        for ing in state.formulation.ingredients:
            entry = {
                "ingredient": ing.name,
                "compound": None,
                "cid": None,
                "identity": {},
                "candidates": [],   # {source, name, verdict, tier, finding, citation}
                "note": "",
            }

            cid = None
            compound = None
            for name in _candidate_names(ing):
                cid = self.pubchem.resolve_cid(name)
                if cid:
                    compound = name
                    break

            if not cid:
                entry["note"] = ("No PubChem compound identity resolved for this "
                                 "ingredient; pathway context unavailable (gap, "
                                 "not a claim of absence).")
                state.pharmacology.append(entry)
                continue

            entry["cid"] = cid
            entry["compound"] = compound
            entry["identity"] = self.pubchem.properties(cid)

            pw = self.pubchem.pathways(cid)
            if not pw.ok:
                state.note_source_failure("PubChem", f"pathways:{ing.name}", pw.error or "")
                entry["note"] = "PubChem pathway lookup unavailable."
                state.pharmacology.append(entry)
                continue

            candidates = _dedupe_pathways(pw.records)[: self.max_pathways]
            if not candidates:
                entry["note"] = ("No curated pathways associated with this "
                                 "compound in PubChem.")
                state.pharmacology.append(entry)
                continue

            for cand in candidates:
                verdict = self._verify(compound, cand["name"], ing.name, category, state)
                entry["candidates"].append({
                    "source": cand["source"],
                    "name": cand["name"],
                    **verdict,
                })
            state.pharmacology.append(entry)

    def _verify(self, compound, pathway_name, ingredient, category, state) -> dict:
        """Search PubMed for {compound} + pathway terms, authenticate with
        Claude, and return a verdict dict."""
        base = {
            "verdict": "UNVERIFIED",
            "tier": EvidenceTier.NONE.label,
            "finding": "",
            "citation": "",
        }
        terms = _query_terms(pathway_name)
        query = f'"{compound}"' + (f" AND {terms}" if terms else "")
        res = self.pubmed.search(query, max_results=5)

        if not res.ok:
            state.note_source_failure("PubMed", f"mechanism:{compound}", res.error or "")
            base["finding"] = ("Literature source unavailable; database "
                               "association not verified.")
            return base
        if not res.records:
            base["verdict"] = "DB_ONLY"
            base["finding"] = ("Database-reported association for the isolated "
                               "compound; no supporting literature retrieved. "
                               "Mechanistic context only — not efficacy.")
            return base

        # No authenticator -> report as database association, unverified.
        if not (self.authenticator and self.authenticator.available):
            base["verdict"] = "DB_ONLY"
            pmids = ", ".join(f"PMID:{r.get('pmid')}" for r in res.records[:3])
            base["finding"] = ("Database-reported association; literature exists "
                               f"({pmids}) but was not independently authenticated.")
            return base

        claim = (f"{compound} modulates the '{pathway_name}' pathway")
        pairs = [{"pmid": str(r.get("pmid", "")), "claim": claim,
                  "ingredient": ingredient} for r in res.records]
        results = self.authenticator.verify_batch(pairs, category=category)
        state.authentication_log.extend(results)

        passed = [a for a in results if a.passed]
        if passed:
            best = max(passed, key=lambda a: int(a.authenticated_tier))
            # Mechanistic findings are capped at PRECLINICAL: a pathway hit is
            # not a human clinical effect, however strong the paper.
            tier = min(best.authenticated_tier, EvidenceTier.PRECLINICAL)
            base["verdict"] = "VERIFIED"
            base["tier"] = tier.label
            base["finding"] = best.actual_finding or best.reasoning
            base["citation"] = f"PMID:{best.pmid}"
        else:
            base["verdict"] = "DB_ONLY"
            rej = next((a for a in results if a.reasoning), None)
            base["finding"] = ("Database-reported association; retrieved papers "
                               "did NOT pass authentication for this pathway. "
                               + (rej.reasoning if rej else ""))
        return base
