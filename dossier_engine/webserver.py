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
authentication when ANTHROPIC_API_KEY is set) and returns the compiled
markdown report, a structured summary the frontend renders as cards, and a
versioned herbenzo-contracts ``Dossier`` JSON (Task T7) for Stage E / glue.

Async jobs (Task T18 / Finding #14):
  POST /api/jobs/analyze → 202 {job_id}; GET /api/jobs/{id} polls progress.
  POST /api/analyze stays synchronous for backward compatibility (glue).
  Stage C UI polls its own jobs — not a mega dashboard.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from .state import FormulationInput, Ingredient, GraphState
from .nodes import LabelDissector, ScripturalNode, RegulatoryNode
from .nodes_v2 import PerIngredientVerifier, SafetyQueryNode
from .pharmacology import MechanismDiscoveryNode
from .compiler_v2 import compile_v2
from .dossier_emit import build_dossier, dossier_to_wire, enum_resolution_meta
from .sources import PubMedClient, PubChemClient, SourceResult
from .authenticator import ClaudeAuthenticator
from .pipeline_health import build_health_payload

PORT = int(os.environ.get("PORT", "8010"))
WEB_DIR = Path(__file__).resolve().parent / "web"
ANALYZE_JOB_TIMEOUT_S = float(os.environ.get("ANALYZE_JOB_TIMEOUT_S", "540"))
ANALYZE_MAX_CONCURRENT_JOBS = int(os.environ.get("ANALYZE_MAX_CONCURRENT_JOBS", "2"))

ProgressCb = Callable[[str, int, str, Optional[Dict[str, Any]]], None]
_JOB_PATH = re.compile(r"^/api/jobs/([0-9a-fA-F-]{36})(/result)?$")


class _NoChembl:
    def lookup(self, name):
        return SourceResult.empty("ChEMBL")


def _build_formulation(payload: dict):
    # Task T6 — shared-contract inbound gate (reject unknown fields / bad schema).
    from .contract_gate import validate_inbound_formulation

    validated = validate_inbound_formulation(payload)
    ingredients = [
        Ingredient(
            name=i.name,
            latin_name=i.latin_name,
            stated_dose=i.stated_dose,
            stated_standardization=i.stated_standardization,
            marker_compound=i.marker_compound,
            marker_is_measured_for_product=i.marker_is_measured_for_product,
        )
        for i in validated.ingredients
    ]
    formulation = FormulationInput(
        product_name=validated.product_name,
        ingredients=ingredients,
        claimed_benefits=list(validated.claimed_benefits),
        target_market=str(validated.target_market),
        dosage_form=validated.dosage_form,
        product_category=validated.product_category or "",
        regulatory_category=(
            str(validated.regulatory_category)
            if validated.regulatory_category is not None
            else None
        ),
        serving_size_g=validated.serving_size_g,
    )
    return formulation, validated


def _progress(
    cb: ProgressCb | None,
    stage: str,
    percent: int,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    if cb is None:
        return
    try:
        cb(stage, percent, message, detail)
    except Exception:  # noqa: BLE001
        pass


def _analyze(payload: dict, *, on_progress: ProgressCb | None = None) -> dict:
    _progress(on_progress, "validate", 5, "Validating formulation…")
    formulation, validated = _build_formulation(payload)
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
    _progress(on_progress, "label", 12, "Dissecting label…")
    LabelDissector().run(state, chembl=_NoChembl())
    _progress(on_progress, "per_ingredient", 30, "Per-ingredient PubMed + auth…")
    PerIngredientVerifier(pubmed, authenticator=auth).run(state)
    _progress(on_progress, "mechanism", 50, "Mechanism discovery…")
    MechanismDiscoveryNode(PubChemClient(), pubmed, authenticator=auth).run(state)
    _progress(on_progress, "safety", 65, "Safety queries…")
    SafetyQueryNode(pubmed, authenticator=auth).run(state)
    _progress(on_progress, "scriptural", 75, "Scriptural node…")
    ScripturalNode().run(state)
    _progress(on_progress, "regulatory", 85, "Regulatory flags…")
    RegulatoryNode().run(state)

    _progress(on_progress, "compile", 92, "Compiling dossier…")
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

    # Task T7 — versioned structured Dossier (markdown remains for humans).
    # Task T12 — consume ModernizedSKU when present (do not discard BCS/delivery/markers).
    from .contract_gate import parse_optional_modernized_sku

    sku = parse_optional_modernized_sku(validated.modernized_sku)
    inbound_thread = None
    if getattr(validated, "provenance_thread", None) is not None:
        pt = validated.provenance_thread
        inbound_thread = pt.model_dump(mode="json") if hasattr(pt, "model_dump") else pt
    dossier = build_dossier(
        state,
        report_md,
        confidence=getattr(validated, "confidence", None),
        inherited_confidence=getattr(validated, "inherited_confidence", None),
        confidence_floor=getattr(validated, "confidence_floor", None),
        modernized_sku=sku,
        modernized_sku_present=sku is not None,
        source_sku_id=sku.sku_id if sku is not None else None,
        source_formulation_id=(
            sku.source_formulation_id if sku is not None else None
        ),
        provenance_thread=inbound_thread,
    )

    enum_meta = enum_resolution_meta(dossier)
    _progress(on_progress, "done", 100, "Complete")

    thread_wire = None
    if dossier.provenance_thread is not None:
        thread_wire = dossier.provenance_thread.model_dump(mode="json")

    return {
        "product_name": formulation.product_name,
        "category": formulation.product_category or "general",
        "regulatory_category": (
            str(dossier.regulatory_category)
            if dossier.regulatory_category is not None
            else None
        ),
        "target_market": formulation.target_market,
        "auth_enabled": auth_enabled,
        "auth_requested": bool(use_auth),
        "report_markdown": report_md,
        "dossier": dossier_to_wire(dossier),
        "enum_resolution": enum_meta,
        "provenance_thread": thread_wire,
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

    def _send_json(self, code: int, obj: dict, *, location: str | None = None):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("  %s\n" % (fmt % args))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            index = WEB_DIR / "index.html"
            try:
                self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, "index.html not found")
            return
        if path == "/api/health":
            auth = ClaudeAuthenticator()
            payload = build_health_payload(
                auth_available=auth.available,
                model=auth.model,
            )
            payload.update(
                {
                    "async_jobs": True,
                    "analyze_job_timeout_s": ANALYZE_JOB_TIMEOUT_S,
                    "analyze_max_concurrent_jobs": ANALYZE_MAX_CONCURRENT_JOBS,
                }
            )
            self._send(200, json.dumps(payload))
            return
        m = _JOB_PATH.match(path)
        if m:
            from . import jobs as jobmod

            job_id, want_result = m.group(1), m.group(2)
            rec = jobmod.STORE.get(job_id)
            if not rec:
                self._send_json(404, {"error": "Unknown job_id"})
                return
            if want_result:
                if rec.status in ("queued", "running"):
                    self._send_json(
                        409,
                        {
                            "error": "Job not finished",
                            "status": rec.status,
                            "progress": {
                                "stage": rec.progress_stage,
                                "percent": rec.progress_percent,
                                "message": rec.progress_message,
                            },
                        },
                    )
                    return
                if rec.status == "succeeded":
                    self._send_json(200, rec.result or {})
                    return
                self._send_json(
                    422,
                    {"error": rec.error or "Job failed", "status": rec.status},
                )
                return
            self._send_json(200, jobmod.STORE.public_view(rec))
            return
        self._send(404, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/jobs/analyze":
            self._handle_job_submit()
            return
        if path != "/api/analyze":
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
        except Exception as e:  # noqa: BLE001 — classify below
            self._send_analyze_error(e)

    def _handle_job_submit(self):
        from . import jobs as jobmod

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {"error": f"Bad request: {e}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "JSON object required"})
            return
        # Validate early so async jobs fail fast (422) instead of after poll.
        try:
            _build_formulation(payload)
        except Exception as e:  # noqa: BLE001
            self._send_analyze_error(e)
            return
        timeout_s = payload.pop("_timeout_s", None)
        try:
            timeout_s = float(timeout_s) if timeout_s is not None else None
        except (TypeError, ValueError):
            timeout_s = None
        idem = self.headers.get("Idempotency-Key")
        rec = jobmod.STORE.submit_analyze(
            payload=payload,
            idempotency_key=idem,
            timeout_s=timeout_s,
            runner=_analyze,
        )
        view = jobmod.STORE.public_view(rec)
        self._send_json(202, view, location=view["poll_url"])

    def _send_analyze_error(self, e: BaseException) -> None:
        try:
            from pydantic import ValidationError
            from .contract_gate import http_error_body
        except ImportError:
            ValidationError = type(None)  # type: ignore[misc, assignment]
            http_error_body = None  # type: ignore[assignment]
        if ValidationError is not type(None) and isinstance(e, ValidationError):
            self._send(422, json.dumps(http_error_body(e)))
            return
        if isinstance(e, ValueError):
            self._send(422, json.dumps({"error": str(e)}))
            return
        traceback.print_exc()
        self._send(500, json.dumps({"error": f"Analysis failed: {e}"}))


def main() -> int:
    auth = ClaudeAuthenticator()
    health = build_health_payload(
        auth_available=auth.available,
        model=auth.model,
    )
    pipeline_mode = os.environ.get("PIPELINE_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    if pipeline_mode and not health["pipeline_ready"]:
        print(
            "\nREFUSED: PIPELINE_MODE requires ANTHROPIC_API_KEY "
            "(safety adjudication). Set the key in .env or unset PIPELINE_MODE "
            "for local UI exploration only.\n",
            file=sys.stderr,
        )
        return 2

    state = (
        f"Claude authentication ENABLED ({auth.model})"
        if auth.available
        else (
            "Claude authentication DISABLED (no ANTHROPIC_API_KEY) — "
            "safety §3 fail-closed; pipeline_ready=false"
        )
    )
    print(f"\nDossier Engine platform (harden-C)  •  {state}")
    print(
        f"  safety_adjudication={health['safety_adjudication']}  "
        f"pipeline_ready={health['pipeline_ready']}"
    )
    if not health["pipeline_ready"]:
        print(
            "  WARNING: not pipeline-ready — glue/operators must not treat "
            "this process as export-trustworthy until ANTHROPIC_API_KEY is set."
        )
    print(f"Open  http://localhost:{PORT}\n")
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
