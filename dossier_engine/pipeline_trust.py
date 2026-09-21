"""
pipeline_trust.py
=================
Controls whether UNVERIFIED (auth-error) PMIDs may soft-retain into
compiled efficacy / mechanism citations.

Modes (env ``PIPELINE_TRUST``):
  - ``strict`` (default, including unset) — omit UNVERIFIED PMIDs from
    citations; surface a gap caveat. Aligns efficacy with safety fail-closed.
  - ``demo`` — legacy soft-retain: keep MeSH-gated paper + UNVERIFIED caveat.

Soft-retain is opt-in only via explicit ``demo`` (aliases: soft, soft-retain).
"""

from __future__ import annotations

import os
from typing import Literal, Optional, Tuple

TrustMode = Literal["strict", "demo"]

_DEMO_ALIASES = frozenset({"demo", "soft", "soft-retain", "soft_retain"})


def resolve_pipeline_trust(value: Optional[str] = None) -> TrustMode:
    """
    Resolve trust mode.

    When ``value`` is None, read ``PIPELINE_TRUST`` from the environment.
    Unknown / empty values resolve to ``strict``.
    """
    raw = os.environ.get("PIPELINE_TRUST", "") if value is None else value
    normalized = (raw or "").strip().lower()
    if normalized in _DEMO_ALIASES:
        return "demo"
    return "strict"


def should_cite_unverified(trust_mode: Optional[str] = None) -> bool:
    """True only in explicit demo mode (soft-retain UNVERIFIED PMIDs)."""
    return resolve_pipeline_trust(trust_mode) == "demo"


def unverified_auth_decision(
    pmid: str,
    error: str,
    *,
    trust_mode: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Decide whether an auth-error (UNVERIFIED) paper enters compiled citations.

    Returns ``(include_in_citations, caveat_text)``.
    """
    pmid_s = str(pmid or "").strip() or "?"
    err = (error or "verification failed").strip()
    if should_cite_unverified(trust_mode):
        caveat = (
            f"PMID:{pmid_s} — Claude authentication UNVERIFIED ({err}); "
            f"paper retained on the MeSH relevance gate but not independently "
            f"confirmed. Manual review recommended."
        )
        return True, caveat
    caveat = (
        f"PMID:{pmid_s} — Claude authentication UNVERIFIED ({err}); "
        f"omitted from citations (PIPELINE_TRUST=strict). "
        f"Manual review required."
    )
    return False, caveat
