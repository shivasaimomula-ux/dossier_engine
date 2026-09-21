"""HTTP/CLI contract gate for Stage C FormulationInput inbound (Task T6/T12).

Outbound structured Dossier emit lives in ``dossier_emit`` (Task T7).
Validates the classical/label FormulationInput stub via herbenzo-contracts
and rejects unknown fields.

When ``modernized_sku`` is present (B→C path), also validates it as
``ModernizedSKU``. Absent/null remains allowed for B-stubbed classical runs.
"""

from __future__ import annotations

from typing import Any, Optional

from herbenzo_contracts import (
    FormulationInput,
    ModernizedSKU,
    validation_error_body,
)


def parse_optional_modernized_sku(raw: Any) -> Optional[ModernizedSKU]:
    """Validate B ModernizedSKU when present; ``None``/absent stays allowed."""
    if raw is None:
        return None
    return ModernizedSKU.model_validate(raw)


def validate_inbound_formulation(payload: dict[str, Any]) -> FormulationInput:
    """Validate C analyze/CLI intake. Raises ValidationError on failure."""
    if not isinstance(payload, dict):
        raise ValueError("formulation payload must be a JSON object")
    # Strip None-only noise but keep known extras; forbid truly unknown keys.
    fi = FormulationInput.model_validate(payload)
    # Task T12 — reject malformed ModernizedSKU early (do not silently discard).
    parse_optional_modernized_sku(fi.modernized_sku)
    return fi


def http_error_body(exc: BaseException) -> dict[str, Any]:
    return validation_error_body(exc)
