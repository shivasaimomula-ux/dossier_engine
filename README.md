# Honest Clinical & Regulatory Evidence Engine

> **Pipeline C (canonical):** this worktree / `harden-c` branch is the **only**
> Stage C build allowed on the Herbenzo glue path. Desktop base
> `~/Desktop/dossier_engine` is **quarantined** (keyword-only safety PMIDs —
> see that tree’s `QUARANTINED.md`). Independent C UI stays here; do not merge
> stage UIs.
>
> Health: `GET /api/health` returns `safety_adjudication: "required"` and
> `pipeline_ready: true` only when `ANTHROPIC_API_KEY` is wired. Set
> `PIPELINE_MODE=1` to refuse startup without the key.
>
> Trust: `PIPELINE_TRUST=strict` (default) omits UNVERIFIED efficacy PMIDs from
> compiled citations (same fail-closed stance as safety). Soft-retain only with
> explicit `PIPELINE_TRUST=demo`.
>
> Handoff (Task T7): `POST /api/analyze` returns `report_markdown` **and** a
> versioned `dossier` object (`herbenzo-contracts` `schema_version: "1.0.0"`)
> for Stage E / glue. Markdown stays for humans; E prefers the structured JSON.

A multi-agent graph that ingests a supplement formulation and produces an
**evidence map** — not a polished marketing dossier. It keeps the orchestrator /
specialist topology from the original spec, but its defining rule is:

> **The engine never invents a value, citation, concentration, or mechanism.
> When a source returns nothing, the field is stamped "NOT ESTABLISHED /
> insufficient data," and that status propagates to the final document.**

## Why this differs from the original spec

The original specification contained instructions that, taken together, build a
fabrication engine: "extract **or assign**" concentration markers, "state a
quantified bioavailability multiplier," require that **every** mechanism claim
carry a PMID (which forces citation-laundering), demand exact pharmacopoeia
page numbers "wherever possible," and emit a "clean, **unhedged**" pharma-grade
dossier. Those produce authoritative-looking output detached from what the data
supports — which misleads consumers and regulators and is non-compliant for a
supplement. This version keeps each agent's legitimate job and removes the
fabrication.

## Architecture

```
FormulationInput
      │
 Orchestrator ──► LabelDissector  (records stated specs; gaps stay gaps)
      │       ──► Bioavailability  (only study-derived figures, with caveats)
      │       ──► ClinicalVerifier (PMID only where a paper supports the claim)
      │  (merge)
      ├──► ScripturalNode  (traditional use ≠ efficacy; no invented citations)
      └──► RegulatoryNode  (flags disease claims & claims above their evidence)
                 │
            compile_dossier  (prints tiers + every gap; refuses weak claims)
```

Improved per-ingredient pipeline (`nodes_v2.py` + `compiler_v2.py`), with the
Claude authentication layer between retrieval and grading:

```
per ingredient:
  PubMed search ──► retraction check ──► MeSH relevance gate
                                              │ (on-purpose papers only)
                                              ▼
                                   ClaudeAuthenticator
                                   (fetch REAL abstract → verify claim)
                                        │        │         │
                                  AUTHENTICATED  REJECTED  UNVERIFIED
                                        │        │         │
                                    graded    dropped   omit (strict)
                                              │              │
                                              │         soft-retain (demo only)
                                              ▼
                                   grade_pubmed_hits ──► compile_v2
```

### Trust mode (`PIPELINE_TRUST`)

| Value | Behavior for efficacy UNVERIFIED (auth error / no confirmation) |
|-------|------------------------------------------------------------------|
| `strict` (default, including unset) | **Omit** PMID from compiled citations; caveat records the gap |
| `demo` | Soft-retain on MeSH gate + UNVERIFIED caveat (legacy exploration) |

Safety §3 cites are always fail-closed (never soft-retain), regardless of this
flag. Mechanism path never emits UNVERIFIED PMIDs as citations either.

### The guardrail layer (`guardrails.py`)
- `EvidenceTier`: NONE → INSUFFICIENT → TRADITIONAL_ONLY → PRECLINICAL →
  PRELIMINARY_HUMAN → ESTABLISHED_HUMAN.
- `EvidenceRecord.is_assertable()`: a claim is only printed as supported if it
  clears a minimum tier **and** carries at least one real citation.
- `grade_pubmed_hits()`: assigns tier from the *study types* actually retrieved
  (a lone review never becomes "established human").
- `flag_if_disease_claim()`: catches treat/cure/prevent phrasing regardless of
  evidence.

### The Claude authentication layer (`authenticator.py`)
A second, independent model verifies every claim-PMID pair **before** it counts
as evidence. When another system (e.g. a Gemini retrieval pass) says "PMID X
supports claim Y", the authenticator:

1. Fetches the **real abstract** for PMID X directly from PubMed's efetch API —
   ground truth, not the retrieving model's description of the paper.
2. Sends the abstract + the claim to Claude via a forced-tool-call
   (`record_verification`) so the answer is schema-constrained, and asks:
   is the claim *supported / partial / unsupported / fabricated*? What is the
   true evidence level (in-vitro / animal / human_pilot / rct / …)? Is this the
   paper's *primary* finding or a *peripheral* mention in the intro/discussion?
3. Rejects pairs where the paper's actual findings don't support the claim,
   downgrades the tier when Claude's study-design read is stricter than the
   metadata suggested, and detects **single-PMID spreading** (one paper cited
   across many unrelated claims).

This catches three failure modes MeSH gating alone cannot: hallucinated PMIDs,
evidence-level inflation (a mouse/in-vitro study presented as human evidence),
and peripheral-mention citation-laundering.

**Design rule preserved:** Claude *interprets and authenticates*; it never
overrides the deterministic guardrails. It cannot upgrade a tier above what the
evidence licenses, cannot clear a disease-claim flag, and a transient auth
failure is surfaced as **UNVERIFIED** (needs review). Under `PIPELINE_TRUST=strict`
(default) that PMID is **omitted** from compiled citations; only
`PIPELINE_TRUST=demo` soft-retains it.

Enable it by setting `ANTHROPIC_API_KEY` (and optionally
`AUTHENTICATOR_MODEL`, default `claude-sonnet-5`). With no key, the pipeline
falls back to the deterministic MeSH relevance gate. Requires the `anthropic`
SDK (`pip install -r requirements.txt`).

## Running it

**Web UI (pipeline-capable):** from this harden tree only:

```
cp .env.example .env          # paste ANTHROPIC_API_KEY
pip install -r requirements.txt
python -m dossier_engine.webserver
# → http://127.0.0.1:8010/api/health
# Expect: "safety_adjudication": "required", "pipeline_ready": true
```

Any formulation from a JSON file (live PubMed + Claude authentication):
```
cp .env.example .env          # then paste your ANTHROPIC_API_KEY into .env
pip install -r requirements.txt
python -m dossier_engine --formulation sample_formulation.json
```
Flags: `--no-auth` (deterministic MeSH gate only), `--output report.md`
(write to a file). With no key set, it auto-falls back to the deterministic
gate. See `sample_formulation.json` for the input shape.

Offline (guardrail demo — no network):
```
python -m dossier_engine.example_run
```

Unit tests:
```
python -m unittest tests.test_pipeline_health tests.test_pipeline_trust -v
```

Live (real citations). The clients use public APIs:
PubMed E-utilities, ChEMBL REST, ClinicalTrials.gov v2. Run where those hosts
are reachable, or pass an `mcp_call` bridge to the Orchestrator to use the
Anthropic PubMed/ChEMBL/Clinical Trials connectors instead. See
`mcp_bridge_stub.py`.

## Limits / honest disclaimers
- This is informational tooling, not medical advice or a regulatory
  certification. A qualified professional must review output before any use.
- The traditional-use and interaction tables are small, illustrative seeds.
  Replace them with verified, citable indexes before production use.
- Tier assignment is heuristic from metadata; it flags strength, it does not
  replace expert appraisal of individual studies.
- Do **not** point glue or operators at `~/Desktop/dossier_engine` (base); that
  tree ships keyword-only safety citations without Claude adjudication.
