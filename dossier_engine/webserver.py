"""
webserver.py
============
A local web platform for the Dossier Engine. Three things in one page:
  1. Overview — what the engine is and its honest-evidence principle.
  2. How it works — the six agents + the Claude authentication layer.
  3. Analyze — enter your own formulation and see the outcome.

Runs on Python's stdlib http.server (no web framework). Start with:

    python -m dossier_engine.webserver      # then open http://localhost:8010

The /api/analyze endpoint runs the full live pipeline (PubMed + Claude
authentication when ANTHROPIC_API_KEY is set) and returns both the compiled
report and a structured summary the frontend renders as cards.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import FormulationInput, Ingredient, GraphState
from .nodes import LabelDissector, ScripturalNode, RegulatoryNode
from .nodes_v2 import PerIngredientVerifier, SafetyQueryNode
from .pharmacology import MechanismDiscoveryNode
from .compiler_v2 import compile_v2
from .sources import PubMedClient, PubChemClient, SourceResult
from .authenticator import ClaudeAuthenticator

PORT = int(os.environ.get("PORT", "8010"))
WEB_DIR = Path(__file__).resolve().parent / "web"

# Base Desktop tree is keyword-only for safety §3 — never pipeline-ready.
_QUARANTINE_BANNER = (
    "QUARANTINED: this Desktop base dossier_engine cites safety PMIDs without "
    "Claude adjudication. Use ~/Desktop/dossier_engine-harden-c for pipeline C."
)


def _pipeline_mode_requested() -> bool:
    for key in ("PIPELINE_MODE", "DOSSIER_PIPELINE"):
        if os.environ.get(key, "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def _health_payload(auth) -> dict:
    """Health body that glue must reject (no safety_adjudication=required)."""
    return {
        "ok": True,
        "auth_available": bool(auth.available),
        "model": auth.model,
        "safety_adjudication": "absent",
        "pipeline_ready": False,
        "quarantined": True,
        "build": "desktop-base",
        "message": _QUARANTINE_BANNER,
    }


class _NoChembl:
    def lookup(self, name):
        return SourceResult.empty("ChEMBL")


def _build_formulation(payload: dict) -> FormulationInput:
    raw_ings = payload.get("ingredients", [])
    ingredients = [
        Ingredient(
            name=(i.get("name") or "").strip(),
            latin_name=(i.get("latin_name") or "").strip() or None,
            stated_dose=(i.get("stated_dose") or "").strip() or None,
            stated_standardization=(i.get("stated_standardization") or "").strip() or None,
        )
        for i in raw_ings
        if (i.get("name") or "").strip()
    ]
    if not ingredients:
        raise ValueError("At least one ingredient with a name is required.")
    claims = [c.strip() for c in payload.get("claimed_benefits", []) if c.strip()]
    return FormulationInput(
        product_name=(payload.get("product_name") or "Unnamed product").strip(),
        ingredients=ingredients,
        claimed_benefits=claims,
        target_market=(payload.get("target_market") or "US").strip(),
        dosage_form=(payload.get("dosage_form") or "").strip() or None,
        product_category=(payload.get("product_category") or "").strip(),
    )


def _analyze(payload: dict) -> dict:
    formulation = _build_formulation(payload)
    use_auth = payload.get("use_auth", True)

    pubmed = PubMedClient(hydrate_full=True)
    auth = None
    auth_enabled = False
    if use_auth:
        candidate = ClaudeAuthenticator(pubmed=pubmed)
        if candidate.available:
            auth = candidate
            auth_enabled = True

    state = GraphState(formulation=formulation)
    LabelDissector().run(state, chembl=_NoChembl())
    PerIngredientVerifier(pubmed, authenticator=auth).run(state)
    MechanismDiscoveryNode(PubChemClient(), pubmed, authenticator=auth).run(state)
    SafetyQueryNode(pubmed).run(state)
    ScripturalNode().run(state)
    RegulatoryNode().run(state)

    report_md = compile_v2(state)

    per_ingredient = [
        {
            "name": ing.name,
            "latin_name": ing.latin_name or "",
            "tier": rec.tier.label,
            "tier_rank": int(rec.tier),
            "citations": [c.render() for c in rec.citations],
            "notes": rec.caveats,
        }
        for ing, rec in state.per_ingredient_evidence
    ]

    auth_log = [
        {
            "ingredient": a.ingredient,
            "pmid": a.pmid,
            "verdict": a.verdict_short,
            "passed": a.passed,
            "claim_supported": a.claim_supported,
            "evidence_level": a.evidence_level,
            "species_or_model": a.species_or_model,
            "sample_size": a.sample_size,
            "actual_finding": a.actual_finding,
            "reasoning": a.reasoning,
            "flags": a.flags,
        }
        for a in state.authentication_log
    ]

    safety = [
        {
            "name": ing.name,
            "tier": rec.tier.label,
            "citations": [c.render() for c in rec.citations],
            "notes": rec.caveats,
        }
        for ing, rec in state.queried_safety
    ]

    return {
        "product_name": formulation.product_name,
        "category": formulation.product_category or "general",
        "target_market": formulation.target_market,
        "auth_enabled": auth_enabled,
        "auth_requested": bool(use_auth),
        "report_markdown": report_md,
        "per_ingredient": per_ingredient,
        "safety": safety,
        "pharmacology": state.pharmacology,
        "authentication_log": auth_log,
        "formulation_flags": state.safety_flags,
        "regulatory_flags": state.regulatory_flags,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("  %s\n" % (fmt % args))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            index = WEB_DIR / "index.html"
            try:
                self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, "index.html not found")
            return
        if self.path == "/api/health":
            auth = ClaudeAuthenticator()
            self._send(200, json.dumps(_health_payload(auth)))
            return
        self._send(404, "Not found")

    def do_POST(self):
        if self.path != "/api/analyze":
            self._send(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, json.dumps({"error": f"Bad request: {e}"}))
            return
        try:
            result = _analyze(payload)
            self._send(200, json.dumps(result))
        except ValueError as e:
            self._send(422, json.dumps({"error": str(e)}))
        except Exception as e:  # noqa: BLE001 — surface to the UI
            traceback.print_exc()
            self._send(500, json.dumps({"error": f"Analysis failed: {e}"}))


def main() -> int:
    print(f"\n*** {_QUARANTINE_BANNER} ***\n", file=sys.stderr)
    if _pipeline_mode_requested():
        print(
            "REFUSED: PIPELINE_MODE / DOSSIER_PIPELINE is set. "
            "Start harden-C instead:\n"
            "  cd ~/Desktop/dossier_engine-harden-c && "
            "python -m dossier_engine.webserver\n",
            file=sys.stderr,
        )
        return 2

    auth = ClaudeAuthenticator()
    state = (
        f"Claude authentication ENABLED ({auth.model})"
        if auth.available
        else "Claude authentication DISABLED (no ANTHROPIC_API_KEY) — "
        "deterministic MeSH gate only"
    )
    print(f"Dossier Engine platform (BASE / QUARANTINED)  •  {state}")
    print("  safety_adjudication=absent  pipeline_ready=false")
    print("  Local exploration only — not for glue / export-trust runs.")
    print(f"Open  http://localhost:{PORT}\n")
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
