"""Thin HTTP client for the blue citation adjudication service (:8011).

Primary path when ADJUDICATION_PREFERRED (N5.1 cost lock-in). Does not import
``herbenzo-adjudication`` — keep harden-C free of a hard package dep.
Uses stdlib urllib (same stack as PubMedClient).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_BASE_URL = "http://127.0.0.1:8011"


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


ADJUDICATION_URL = (
    os.environ.get("ADJUDICATION_URL") or DEFAULT_BASE_URL
).rstrip("/")
ADJUDICATION_PREFERRED = _env_flag("ADJUDICATION_PREFERRED", "1")
ADJUDICATION_TIMEOUT_S = float(os.environ.get("ADJUDICATION_TIMEOUT_S", "8"))

# Cost order exposed on health (matches blue service semantics).
COST_ORDER = ("lexical", "nim", "gemini", "claude_hard_reject_only")


class AdjudicationClient:
    """Minimal GET /health + POST /adjudicate against the blue service."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or ADJUDICATION_URL).rstrip("/")
        self.timeout_s = (
            timeout_s if timeout_s is not None else ADJUDICATION_TIMEOUT_S
        )

    def health(self) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=min(self.timeout_s, 3.0)) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def is_reachable(self) -> bool:
        try:
            data = self.health()
            return bool(
                data.get("status") == "ok" or data.get("port_contract") == 8011
            )
        except Exception:  # noqa: BLE001 — treat any failure as unreachable
            return False

    def adjudicate(
        self,
        claim: str,
        product: str,
        pmid: str,
        *,
        product_aliases: Optional[list[str]] = None,
        claim_domain: str = "general",
        article: Optional[dict[str, Any]] = None,
        mode: str = "auto",
        force_hard_reject: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "claim": claim,
            "product": product,
            "pmid": str(pmid),
            "product_aliases": product_aliases or [],
            "claim_domain": claim_domain,
            "article": article,
            "mode": mode,
            "force_hard_reject": force_hard_reject,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/adjudicate",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Adjudication HTTP {exc.code}: {detail[:200]}"
            ) from exc
