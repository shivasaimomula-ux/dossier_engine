"""
pipeline_health.py
==================
Health / readiness contract for Stage C (dossier engine).

Harden-C builds ALWAYS advertise ``safety_adjudication: "required"``.
``pipeline_ready`` is True only when the Claude authenticator is wired
(API key present). Glue and operators must refuse non-adjudicating C trees.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Literal advertised by this tree. Base / pre-harden builds must not emit this.
SAFETY_ADJUDICATION_REQUIRED = "required"


def build_health_payload(
    *,
    auth_available: bool,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the JSON body for GET /api/health on harden-C."""
    return {
        "ok": True,
        "auth_available": bool(auth_available),
        "model": model,
        "safety_adjudication": SAFETY_ADJUDICATION_REQUIRED,
        # Never claim pipeline-ready without the authenticator wired.
        "pipeline_ready": bool(auth_available),
        "build": "harden-c",
    }


def is_pipeline_ready(payload: Dict[str, Any]) -> bool:
    """True only when a C health body is adjudicating and authenticator-ready."""
    if not isinstance(payload, dict):
        return False
    if payload.get("safety_adjudication") != SAFETY_ADJUDICATION_REQUIRED:
        return False
    return bool(payload.get("pipeline_ready")) and bool(payload.get("auth_available"))
