"""
state.py
========
The shared state object that flows through the graph. Mirrors the original
spec's "strict Pydantic JSON schema" idea, but every analytical field is an
EvidenceRecord (or a collection of them) so that uncertainty is structural,
not an afterthought.

Uses dataclasses to keep zero hard dependencies; swap in pydantic.BaseModel
directly if you prefer runtime validation — the field layout is identical.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from .guardrails import EvidenceRecord


@dataclasses.dataclass
class Ingredient:
    """A single line item from the product label / certificate of analysis.

    `stated_standardization` and `stated_dose` are ONLY populated from what the
    label/CoA actually says. If the label is silent, they stay None and the
    Label Dissector marks them NOT_SPECIFIED rather than inventing a spec.
    """
    name: str
    latin_name: Optional[str] = None
    stated_dose: Optional[str] = None             # e.g. "500 mg" — as printed
    stated_standardization: Optional[str] = None  # e.g. "95% curcuminoids" — as printed
    marker_compound: Optional[str] = None         # filled by ChEMBL lookup (reference only)
    marker_is_measured_for_product: bool = False  # almost always False


@dataclasses.dataclass
class FormulationInput:
    product_name: str
    ingredients: list                  # list[Ingredient]
    claimed_benefits: list             # list[str], as marketed
    target_market: str = "US"          # drives which regulatory frame applies
    dosage_form: Optional[str] = None  # "capsule", "softgel", "phytosome", ...
    product_category: str = ""         # MeSH / indication: "laxative", "anti-inflammatory"
    # Shared E regulatory lens (Task T8). None → resolve from product_category aliases.
    regulatory_category: Optional[str] = None
    serving_size_g: Optional[float] = None  # grams per serving (for dose calc)


@dataclasses.dataclass
class GraphState:
    """Accumulates as nodes run. Each analytical section is a list of
    EvidenceRecords so the compiler can refuse anything below threshold."""
    formulation: FormulationInput
    # Node 2
    label_analysis: list = dataclasses.field(default_factory=list)
    label_gaps: list = dataclasses.field(default_factory=list)
    # Node 3
    bioavailability: list = dataclasses.field(default_factory=list)
    # Node 4
    mechanism_evidence: list = dataclasses.field(default_factory=list)
    benefit_evidence: list = dataclasses.field(default_factory=list)
    # Node 5
    traditional_use: list = dataclasses.field(default_factory=list)
    # Node 6
    safety_flags: list = dataclasses.field(default_factory=list)
    regulatory_flags: list = dataclasses.field(default_factory=list)
    # Per-ingredient evidence (Priority 1 fix)
    per_ingredient_evidence: list = dataclasses.field(default_factory=list)
    # Queried safety findings (Priority 2 fix — replaces static dict)
    queried_safety: list = dataclasses.field(default_factory=list)
    # PubChem-derived candidate pathways, verified through PubMed + Claude
    pharmacology: list = dataclasses.field(default_factory=list)
    # Audit trail: every claim-evidence link (Priority 1 fix)
    audit_trail: list = dataclasses.field(default_factory=list)
    # Claude authentication results for every claim-PMID pair
    authentication_log: list = dataclasses.field(default_factory=list)
    # provenance of source failures (so gaps are explained honestly)
    source_failures: list = dataclasses.field(default_factory=list)

    def note_source_failure(self, source: str, context: str, error: str) -> None:
        self.source_failures.append(
            {"source": source, "context": context, "error": error}
        )
