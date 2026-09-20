"""HTTP/CLI contract gate for Stage C FormulationInput inbound (Task T6).

Outbound structured Dossier emit lives in ``dossier_emit`` (Task T7).
Validates the classical/label FormulationInput stub via herbenzo-contracts
and rejects unknown fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from herbenzo_contracts import FormulationInput, validation_error_body


def validate_inbound_formulation(payload: dict[str, Any]) -> FormulationInput:
    """Validate C analyze/CLI intake. Raises ValidationError on failure."""
    if not isinstance(payload, dict):
        raise ValueError("formulation payload must be a JSON object")
    # Strip None-only noise but keep known extras; forbid truly unknown keys.
    return FormulationInput.model_validate(payload)


def http_error_body(exc: BaseException) -> dict[str, Any]:
    return validation_error_body(exc)
