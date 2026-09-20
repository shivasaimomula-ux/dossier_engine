"""Emit versioned herbenzo-contracts ``Dossier`` from GraphState (Task T7).

Markdown remains the human companion; the structured payload is the C→E
machine handoff. Does not expand category enums (Task T8).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Optional

from herbenzo_contracts import (
    Dossier,
    DossierEvidenceItem,
    DossierIngredient,
    DossierSection,
)

from .guardrails import EvidenceRecord, EvidenceTier
from .state import GraphState

ENGINE_VERSION = "C-dossier/1.1.0"

_AUTH_STATUS = {
    "supported": "supported",
    "partial": "partial",
    "unsupported": "unsupported",
    "fabricated": "refuted",
}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:48] or "product"


def _dossier_id(product_name: str) -> str:
    slug = _slug(product_name)
    short = uuid.uuid4().hex[:8]
    return f"D-{slug}-{short}"


def _tier_status(tier: EvidenceTier) -> str:
    if tier in (EvidenceTier.NONE, EvidenceTier.INSUFFICIENT):
        return "gap"
    if tier == EvidenceTier.TRADITIONAL_ONLY:
        return "partial"
    return "supported"


def _pmid_from_citation(citation: Any) -> Optional[str]:
    ident = getattr(citation, "identifier", None) or ""
    source = (getattr(citation, "source", None) or "").lower()
    if "pubmed" in source or ident.isdigit():
        return str(ident) if ident else None
    if ident.upper().startswith("PMID"):
        return ident.split(":")[-1].strip() or None
    return None


def _items_from_record(rec: EvidenceRecord) -> list[DossierEvidenceItem]:
    claim = (rec.claim or rec.subject or "evidence").strip() or "evidence"
    if not rec.citations:
        return [
            DossierEvidenceItem(
                claim=claim,
                tier=rec.tier.label,
                status=_tier_status(rec.tier),
                note="; ".join(rec.caveats) if rec.caveats else None,
            )
        ]
    items: list[DossierEvidenceItem] = []
    for cit in rec.citations:
        note_bits = []
        if getattr(cit, "note", None):
            note_bits.append(cit.note)
        if rec.caveats:
            note_bits.extend(rec.caveats)
        items.append(
            DossierEvidenceItem(
                claim=claim,
                pmid=_pmid_from_citation(cit),
                tier=rec.tier.label,
                status=_tier_status(rec.tier),
                note="; ".join(note_bits) if note_bits else None,
            )
        )
    return items


def _auth_status(auth: Any) -> str:
    if getattr(auth, "error", None):
        return "unverified"
    supported = getattr(auth, "claim_supported", "unsupported") or "unsupported"
    return _AUTH_STATUS.get(supported, "unsupported")


def _confidence_pair(
    payload_confidence: Optional[float],
    payload_inherited: Optional[float],
    payload_floor: Optional[float],
) -> tuple[float, float]:
    """Pick confidence that never raises the inherited floor."""
    inherited = 1.0
    if payload_inherited is not None:
        inherited = float(payload_inherited)
    elif payload_floor is not None:
        inherited = float(payload_floor)

    if payload_confidence is not None:
        confidence = min(float(payload_confidence), inherited)
    elif payload_floor is not None:
        confidence = min(float(payload_floor), inherited)
    else:
        # Classical path without a floor: honest mid confidence, floor = 1.0.
        confidence = min(0.7, inherited)
    return confidence, inherited


def build_dossier(
    state: GraphState,
    report_markdown: str,
    *,
    confidence: Optional[float] = None,
    inherited_confidence: Optional[float] = None,
    confidence_floor: Optional[float] = None,
    dossier_id: Optional[str] = None,
    source_formulation_id: Optional[str] = None,
    source_sku_id: Optional[str] = None,
    modernized_sku_present: bool = False,
    engine_version: str = ENGINE_VERSION,
) -> Dossier:
    """Compile GraphState + markdown companion into a versioned ``Dossier``."""
    formulation = state.formulation
    conf, inherited = _confidence_pair(
        confidence, inherited_confidence, confidence_floor
    )

    ingredients = [
        DossierIngredient(
            name=ing.name,
            latin_name=ing.latin_name,
            stated_dose=ing.stated_dose,
            stated_standardization=ing.stated_standardization,
            marker_compound=ing.marker_compound,
            marker_is_measured_for_product=bool(
                ing.marker_is_measured_for_product
            ),
        )
        for ing in formulation.ingredients
    ]
    if not ingredients:
        raise ValueError("Dossier requires at least one ingredient")

    sections: list[DossierSection] = []

    # Per-ingredient efficacy
    per_items: list[DossierEvidenceItem] = []
    per_gaps: list[str] = []
    for ing, rec in state.per_ingredient_evidence:
        per_items.extend(_items_from_record(rec))
        if rec.tier in (EvidenceTier.NONE, EvidenceTier.INSUFFICIENT):
            per_gaps.append(f"{ing.name}: {rec.tier.label}")
        for cave in rec.caveats:
            if cave not in per_gaps:
                per_gaps.append(cave)
    sections.append(
        DossierSection(
            key="per_ingredient_efficacy",
            title="Per-ingredient efficacy",
            items=per_items,
            gaps=per_gaps,
        )
    )

    # Safety
    safety_items: list[DossierEvidenceItem] = []
    safety_gaps: list[str] = list(state.safety_flags or [])
    for ing, rec in state.queried_safety:
        safety_items.extend(_items_from_record(rec))
        if rec.tier in (EvidenceTier.NONE, EvidenceTier.INSUFFICIENT):
            safety_gaps.append(f"{ing.name}: {rec.tier.label}")
    sections.append(
        DossierSection(
            key="safety",
            title="Safety",
            items=safety_items,
            gaps=safety_gaps,
        )
    )

    # Authentication log (machine-checkable auth verdicts)
    auth_items: list[DossierEvidenceItem] = []
    auth_gaps: list[str] = []
    for auth in state.authentication_log:
        status = _auth_status(auth)
        note_bits = []
        if getattr(auth, "verdict_short", None):
            note_bits.append(f"verdict={auth.verdict_short}")
        if getattr(auth, "reasoning", None):
            note_bits.append(auth.reasoning)
        if getattr(auth, "flags", None):
            note_bits.extend(list(auth.flags))
        if getattr(auth, "error", None):
            note_bits.append(str(auth.error))
            auth_gaps.append(f"PMID:{auth.pmid} UNVERIFIED")
        elif status in ("unsupported", "refuted", "unverified", "gap"):
            auth_gaps.append(
                f"PMID:{auth.pmid} {getattr(auth, 'verdict_short', status)}"
            )
        auth_items.append(
            DossierEvidenceItem(
                claim=(getattr(auth, "claim", None) or "auth claim").strip()
                or "auth claim",
                pmid=str(getattr(auth, "pmid", "") or "") or None,
                tier=getattr(auth, "evidence_level", None),
                status=status,  # type: ignore[arg-type]
                note="; ".join(note_bits) if note_bits else None,
                confidence=None,
            )
        )
    sections.append(
        DossierSection(
            key="authentication",
            title="Citation authentication",
            items=auth_items,
            gaps=auth_gaps,
        )
    )

    # Pharmacology (dict rows from MechanismDiscoveryNode)
    pharm_items: list[DossierEvidenceItem] = []
    for row in state.pharmacology or []:
        if not isinstance(row, dict):
            continue
        name = row.get("ingredient") or row.get("name") or "compound"
        for cand in row.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            verdict = (cand.get("verdict") or "gap").upper()
            if verdict == "VERIFIED":
                status = "supported"
            elif verdict in ("PARTIAL", "DB_ONLY"):
                status = "partial"
            elif verdict == "UNVERIFIED":
                status = "unverified"
            else:
                status = "gap"
            cit = cand.get("citation") or ""
            pmid = None
            if "PMID:" in str(cit).upper():
                pmid = str(cit).split(":")[-1].strip() or None
            pharm_items.append(
                DossierEvidenceItem(
                    claim=f"{name}: {cand.get('name') or cand.get('pathway') or 'pathway'}",
                    pmid=pmid,
                    tier=cand.get("tier"),
                    status=status,  # type: ignore[arg-type]
                    note=cand.get("finding") or cand.get("note"),
                )
            )
    sections.append(
        DossierSection(
            key="pharmacology",
            title="Pharmacology / mechanism",
            items=pharm_items,
            gaps=[],
        )
    )

    # Regulatory flags as gaps (claims / market framing)
    reg_gaps = [str(f) for f in (state.regulatory_flags or [])]
    sections.append(
        DossierSection(
            key="regulatory",
            title="Regulatory flags",
            items=[],
            gaps=reg_gaps,
        )
    )

    category = (formulation.product_category or "").strip() or "general"
    market = str(formulation.target_market or "US")

    return Dossier(
        dossier_id=dossier_id or _dossier_id(formulation.product_name),
        product_name=formulation.product_name,
        product_category=category,
        target_market=market,  # type: ignore[arg-type]
        dosage_form=formulation.dosage_form,
        serving_size_g=formulation.serving_size_g,
        claimed_benefits=list(formulation.claimed_benefits or []),
        ingredients=ingredients,
        sections=sections,
        markdown=report_markdown or None,
        source_formulation_id=source_formulation_id,
        source_sku_id=source_sku_id,
        modernized_sku_present=modernized_sku_present,
        confidence=conf,
        inherited_confidence=inherited,
        source_stage="C:dossier",
        engine_version=engine_version,
    )


def dossier_to_wire(dossier: Dossier) -> dict[str, Any]:
    """JSON-serializable dict (schema_version 1.0.0)."""
    return dossier.model_dump(mode="json")


def content_fingerprint(dossier: Dossier) -> str:
    """Stable-ish hash of structured content (excludes generated_at / id noise)."""
    payload = dossier.model_dump(mode="json", exclude={"generated_at", "dossier_id"})
    raw = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]
