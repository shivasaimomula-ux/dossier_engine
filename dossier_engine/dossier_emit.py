"""Emit versioned herbenzo-contracts ``Dossier`` from GraphState (Task T7/T8/T12).

Markdown remains the human companion; the structured payload is the C→E
machine handoff. Task T8 attaches ``regulatory_category`` via shared enums.
Task T12 consumes optional B ``ModernizedSKU`` (BCS / delivery / markers).
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Optional

from herbenzo_contracts import (
    Dossier,
    DossierEvidenceItem,
    DossierIngredient,
    DossierSection,
    ModernizedIngredient,
    ModernizedSKU,
    resolve_regulatory_category,
    resolve_target_market,
)
from herbenzo_contracts.provenance import (
    dossier_content_hash,
    extend_from_handoff,
)

from .guardrails import EvidenceRecord, EvidenceTier
from .state import GraphState

ENGINE_VERSION = "C-dossier/1.2.0"

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


def _norm_name(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _match_sku_ingredient(
    label_name: str,
    latin_name: Optional[str],
    sku_ings: list[ModernizedIngredient],
    used: set[int],
) -> Optional[ModernizedIngredient]:
    """Best-effort label ↔ ModernizedIngredient match (name / botanical)."""
    label = _norm_name(label_name)
    latin = _norm_name(latin_name)
    for idx, mi in enumerate(sku_ings):
        if idx in used:
            continue
        bot = _norm_name(mi.botanical_name)
        if latin and (latin == bot or latin in bot or bot in latin):
            used.add(idx)
            return mi
        if label and (label == bot or bot in label or label in bot):
            used.add(idx)
            return mi
    return None


def _enrich_ingredients_from_sku(
    ingredients: list[DossierIngredient],
    sku: ModernizedSKU,
) -> list[DossierIngredient]:
    """Overlay HB-* id, quantity_mg, and marker from B when names align."""
    used: set[int] = set()
    enriched: list[DossierIngredient] = []
    for ing in ingredients:
        mi = _match_sku_ingredient(
            ing.name, ing.latin_name, list(sku.ingredients), used
        )
        if mi is None:
            enriched.append(ing)
            continue
        marker_name = mi.marker.marker_name if mi.marker else None
        enriched.append(
            ing.model_copy(
                update={
                    "ingredient_id": mi.ingredient_id or ing.ingredient_id,
                    "quantity_mg": mi.quantity_mg
                    if mi.quantity_mg is not None
                    else ing.quantity_mg,
                    "marker_compound": marker_name or ing.marker_compound,
                    "latin_name": ing.latin_name or mi.botanical_name,
                }
            )
        )
    return enriched


def _sections_from_modernized_sku(sku: ModernizedSKU) -> list[DossierSection]:
    """Compile BCS class, delivery system, and markers into dossier sections."""
    marker_items: list[DossierEvidenceItem] = []
    bcs_items: list[DossierEvidenceItem] = []
    delivery_items: list[DossierEvidenceItem] = []
    marker_gaps: list[str] = []
    bcs_gaps: list[str] = []
    delivery_gaps: list[str] = []

    for mi in sku.ingredients:
        label = mi.botanical_name or mi.ingredient_id
        marker = mi.marker
        props = marker.properties
        marker_note = (
            f"ingredient_id={mi.ingredient_id}; "
            f"is_proxy={marker.is_proxy}; "
            f"pubchem_cid={props.pubchem_cid}; "
            f"rationale={marker.rationale}"
        )
        if mi.marker_dose_mg is not None:
            marker_note += f"; marker_dose_mg={mi.marker_dose_mg}"
        marker_items.append(
            DossierEvidenceItem(
                claim=f"{label}: marker {marker.marker_name}",
                status="supported" if not marker.is_proxy else "partial",
                note=marker_note,
                confidence=None,
            )
        )
        if marker.is_proxy:
            marker_gaps.append(
                f"{label}: marker {marker.marker_name} is a proxy for multi-constituent extract"
            )

        bcs = mi.bcs
        bcs_class = (
            bcs.bcs_class.value
            if hasattr(bcs.bcs_class, "value")
            else str(bcs.bcs_class)
        )
        bcs_items.append(
            DossierEvidenceItem(
                claim=f"{label}: BCS class {bcs_class}",
                status="supported",
                note=(
                    f"solubility={bcs.solubility_call}; "
                    f"permeability={bcs.permeability_call}; "
                    f"evidence_basis={bcs.evidence_basis}; "
                    + "; ".join(bcs.rationale)
                ),
                confidence=bcs.confidence,
            )
        )
        if bcs.is_regulatory_determination is False:
            bcs_gaps.append(
                f"{label}: BCS {bcs_class} is advisory (not a regulatory determination)"
            )

        delivery = mi.delivery
        primary = (
            delivery.primary.value
            if hasattr(delivery.primary, "value")
            else str(delivery.primary)
        )
        alts = [
            a.value if hasattr(a, "value") else str(a)
            for a in (delivery.alternatives or [])
        ]
        bio = delivery.bioavailability
        bio_bits: list[str] = []
        if bio.qualitative_expectation:
            bio_bits.append(bio.qualitative_expectation)
        if bio.fold_change is not None:
            bio_bits.append(f"fold_change={bio.fold_change}")
        if bio.pmid:
            bio_bits.append(f"PMID:{bio.pmid}")
        delivery_note = (
            f"primary={primary}"
            + (f"; alternatives={','.join(alts)}" if alts else "")
            + (f"; excipients={','.join(delivery.excipients)}" if delivery.excipients else "")
            + (f"; bioavailability={'; '.join(bio_bits)}" if bio_bits else "")
            + "; "
            + "; ".join(delivery.rationale)
            + "; advisory_only=true"
        )
        delivery_items.append(
            DossierEvidenceItem(
                claim=f"{label}: delivery {primary}",
                pmid=bio.pmid,
                tier=(
                    bio.evidence_tier.value
                    if bio.evidence_tier is not None
                    and hasattr(bio.evidence_tier, "value")
                    else (str(bio.evidence_tier) if bio.evidence_tier else None)
                ),
                status="partial",  # delivery is advisory_only
                note=delivery_note,
                confidence=None,
            )
        )
        delivery_gaps.append(
            f"{label}: delivery recommendation is advisory_only (active unchanged)"
        )

    return [
        DossierSection(
            key="markers",
            title="Marker compounds (from ModernizedSKU)",
            items=marker_items,
            gaps=marker_gaps,
        ),
        DossierSection(
            key="bcs",
            title="BCS assessment (from ModernizedSKU)",
            items=bcs_items,
            gaps=bcs_gaps,
        ),
        DossierSection(
            key="delivery",
            title="Delivery system (from ModernizedSKU)",
            items=delivery_items,
            gaps=delivery_gaps,
        ),
    ]


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
    modernized_sku: Optional[ModernizedSKU] = None,
    modernized_sku_present: bool = False,
    engine_version: str = ENGINE_VERSION,
    provenance_thread: Optional[dict[str, Any]] = None,
) -> Dossier:
    """Compile GraphState + markdown companion into a versioned ``Dossier``.

    When ``modernized_sku`` is provided (B→C), BCS / delivery / markers are
    compiled into dossier sections and ingredient lines are enriched. When
    absent, classical/label path still emits a valid Dossier (B-stubbed runs).
    """
    formulation = state.formulation

    sku = modernized_sku
    if sku is not None:
        modernized_sku_present = True
        source_sku_id = source_sku_id or sku.sku_id
        source_formulation_id = source_formulation_id or sku.source_formulation_id
        if confidence is None:
            confidence = sku.confidence
        if inherited_confidence is None:
            inherited_confidence = sku.inherited_confidence
        if provenance_thread is None and getattr(sku, "provenance_thread", None) is not None:
            pt = sku.provenance_thread
            provenance_thread = (
                pt.model_dump(mode="json") if hasattr(pt, "model_dump") else pt
            )

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
    if sku is not None:
        ingredients = _enrich_ingredients_from_sku(ingredients, sku)

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

    # Task T12 — BCS / delivery / markers from B ModernizedSKU (when present).
    if sku is not None:
        sections.extend(_sections_from_modernized_sku(sku))

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

    # Task T8: resolve shared regulatory lens. Prefer explicit field; else
    # try product_category aliases (dietary_supplement → SUPPLEMENT). MeSH
    # indications like anti-inflammatory stay on product_category and drop
    # regulatory_category with a documented warning (no silent SUPPLEMENT).
    reg_raw = getattr(formulation, "regulatory_category", None)
    if not reg_raw:
        reg_raw = category if category != "general" else None
    cat_res = resolve_regulatory_category(reg_raw)
    mkt_res = resolve_target_market(market, for_e=False)
    regulatory = cat_res["value"]
    market_out = mkt_res["value"] or market

    dossier = Dossier(
        dossier_id=dossier_id or _dossier_id(formulation.product_name),
        product_name=formulation.product_name,
        product_category=category,
        regulatory_category=regulatory,  # type: ignore[arg-type]
        target_market=market_out,  # type: ignore[arg-type]
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
    # Task T22 — echo upstream IDs + dossier_hash on the run-thread.
    dhash = dossier_content_hash(dossier)
    thread = extend_from_handoff(
        provenance_thread,
        stage="C",
        payload={
            "formulation_id": source_formulation_id,
            "sku_id": source_sku_id,
            "dossier_id": dossier.dossier_id,
        },
        dossier_id=dossier.dossier_id,
        dossier_hash=dhash,
        formulation_id=source_formulation_id,
        sku_id=source_sku_id,
    )
    dossier = dossier.model_copy(update={"provenance_thread": thread})
    # Stash resolution for callers that want _meta-style diagnostics without
    # polluting the strict Dossier schema.
    dossier._category_resolution = {  # type: ignore[attr-defined]
        "category": cat_res["value"],
        "categoryLabel": cat_res["label"],
        "input": cat_res["input"],
        "mappedFrom": cat_res["mapped_from"],
        "recognized": cat_res["recognized"],
        "dropped": cat_res["dropped"],
        "warning": cat_res["warning"],
    }
    dossier._market_resolution = {  # type: ignore[attr-defined]
        "market": mkt_res["value"],
        "marketLabel": mkt_res["label"],
        "input": mkt_res["input"],
        "mappedFrom": mkt_res["mapped_from"],
        "recognized": mkt_res["recognized"],
        "dropped": mkt_res["dropped"],
        "warning": mkt_res["warning"],
    }
    return dossier


def dossier_to_wire(dossier: Dossier) -> dict[str, Any]:
    """JSON-serializable dict (schema_version 1.0.0).

    Enum resolution diagnostics live on the object as ``_category_resolution`` /
    ``_market_resolution`` attributes — callers may attach them beside the
    dossier in the HTTP envelope, not inside the strict schema payload.
    """
    return dossier.model_dump(mode="json")


def enum_resolution_meta(dossier: Dossier) -> dict[str, Any]:
    """Extract T8 resolution dicts for HTTP / glue envelopes."""
    out: dict[str, Any] = {}
    cat = getattr(dossier, "_category_resolution", None)
    mkt = getattr(dossier, "_market_resolution", None)
    if cat is not None:
        out["categoryResolution"] = cat
    if mkt is not None:
        out["marketResolution"] = mkt
    return out


def content_fingerprint(dossier: Dossier) -> str:
    """Stable hash of structured content (full sha256; Task T22 dossier_hash)."""
    if dossier.provenance_thread and dossier.provenance_thread.dossier_hash:
        return dossier.provenance_thread.dossier_hash
    return dossier_content_hash(dossier)
