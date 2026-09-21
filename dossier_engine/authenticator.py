"""
authenticator.py
================
Claude-based verification layer for PubMed claim-evidence pairs.

Sits between retrieval (by any LLM — Gemini, GPT, or the engine's own
PubMed client) and evidence grading. Every claim-PMID pair must pass
Claude authentication before it counts as supporting evidence.

Claude reads the REAL abstract fetched directly from PubMed's own API —
not from the retrieving LLM's description — and verifies:
  1. Does the PMID resolve to a real paper?
  2. Does the paper actually support the specific claim being made?
  3. What is the true evidence level (in-vitro / animal / human)?
  4. Is this the paper's primary finding or a peripheral mention?
  5. Is the same PMID being over-applied across unrelated claims?

When the authenticator is unavailable (no API key), claims are marked
UNVERIFIED and pass through to the existing deterministic guardrails.
"""

from __future__ import annotations

import json
import os
import re
import dataclasses
from pathlib import Path
from typing import Optional

from .guardrails import EvidenceTier
from .sources import PubMedClient


def _warn_env_txt_trap(root: Path, env_path: Path) -> None:
    """Audit #23 / Task T24: base C historically used .env.txt (often a raw
    key blob). Runtime loads only project-root .env as KEY=value lines."""
    env_txt = root / ".env.txt"
    if not env_txt.exists():
        return
    if env_path.exists():
        print(
            "Note: .env.txt is ignored — secrets load from .env only "
            "(KEY=value). Prefer deleting .env.txt after migrating.",
            flush=True,
        )
        return
    print(
        "WARNING: found .env.txt but no .env. Runtime does NOT load "
        ".env.txt. Migrate: cp .env.example .env && paste "
        "ANTHROPIC_API_KEY=… as KEY=value (see README / SECRETS.md).",
        flush=True,
    )


def _load_dotenv() -> None:
    """Populate os.environ from a project-root .env file (KEY=VALUE lines).
    Already-set environment variables win, so an explicit `export` still
    overrides the file. Silently does nothing if .env is absent. Lets local
    testing keep the API key out of shell history.

    Does **not** read `.env.txt` (Audit Finding #23 trap)."""
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    _warn_env_txt_trap(root, env_path)
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if key and os.environ.get(key) is None:
            os.environ[key] = value


_load_dotenv()

# Some models occasionally leak their tool-call scaffolding (e.g. a stray
# "</reasoning>" or "<parameter ...>") into a string field. The leak always
# begins at the first tag-like fragment, so truncate there, then collapse
# whitespace. A tag pattern requires a letter right after "<"/"</", so real
# text such as "p < 0.05" is left untouched.
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_WS_RE = re.compile(r"\s+")


def _clean(text) -> str:
    if text is None:
        return ""
    s = str(text)
    m = _TAG_RE.search(s)
    if m:
        s = s[:m.start()]
    return _WS_RE.sub(" ", s).strip()


AUTHENTICATOR_MODEL = os.environ.get("AUTHENTICATOR_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = (
    "You are a biomedical evidence authenticator. Your job is to verify "
    "whether a scientific paper ACTUALLY supports a specific claim about "
    "a dietary supplement ingredient (efficacy OR safety/interaction).\n\n"
    "You will receive the paper's real abstract (fetched directly from "
    "PubMed) and a claim that another system is attributing to it.\n\n"
    "Rules — be strict:\n"
    "- A paper that MENTIONS a pathway in its introduction or discussion "
    "is NOT evidence for that pathway. Only the paper's own experimental "
    "findings count.\n"
    "- An in-vitro cell-line study does NOT support human efficacy claims.\n"
    "- An animal study does NOT demonstrate an effect in humans.\n"
    "- A narrative review that cites other studies is NOT primary evidence.\n"
    "- A study on a DIFFERENT species, organ, or condition is off-target "
    "even if the same compound is involved.\n"
    "- For safety/interaction claims: only count findings about adverse "
    "effects, toxicity, contraindications, or drug interactions — not "
    "unrelated efficacy results for the same ingredient.\n"
    "- If the abstract does not contain enough information to verify the "
    "claim, say so — do not assume support.\n\n"
    "Respond with valid JSON only, no markdown fences."
)

VERIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "pmid_valid": {
            "type": "boolean",
            "description": "Does this appear to be a real, non-retracted paper?",
        },
        "claim_supported": {
            "type": "string",
            "enum": ["supported", "partial", "unsupported", "fabricated"],
            "description": (
                "supported = the paper's findings directly back the claim; "
                "partial = tangentially related but not a direct finding; "
                "unsupported = paper exists but doesn't support this claim; "
                "fabricated = PMID doesn't resolve or abstract contradicts"
            ),
        },
        "evidence_level": {
            "type": "string",
            "enum": [
                "in_vitro", "animal", "human_pilot", "rct",
                "meta_analysis", "systematic_review", "narrative_review",
                "case_report", "unknown",
            ],
        },
        "finding_type": {
            "type": "string",
            "enum": ["primary", "secondary", "peripheral", "not_mentioned"],
            "description": (
                "primary = paper's main research question; "
                "secondary = a secondary outcome; "
                "peripheral = mentioned in intro/discussion only; "
                "not_mentioned = claim topic not in the paper at all"
            ),
        },
        "actual_finding": {
            "type": "string",
            "description": "One sentence: what the paper actually found",
        },
        "species_or_model": {
            "type": "string",
            "description": "What was studied: human, mouse, rat, cell line name, etc.",
        },
        "sample_size": {
            "type": ["integer", "null"],
            "description": "N for human studies; null for non-human",
        },
        "reasoning": {
            "type": "string",
            "description": "Why the claim is or isn't supported",
        },
        "flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concerns: e.g. 'in_vitro_only', 'wrong_species', 'peripheral_mention'",
        },
    },
    "required": [
        "pmid_valid", "claim_supported", "evidence_level", "finding_type",
        "actual_finding", "species_or_model", "reasoning", "flags",
    ],
}

_LEVEL_TO_TIER = {
    "in_vitro": EvidenceTier.PRECLINICAL,
    "animal": EvidenceTier.PRECLINICAL,
    "case_report": EvidenceTier.PRELIMINARY_HUMAN,
    "human_pilot": EvidenceTier.PRELIMINARY_HUMAN,
    "narrative_review": EvidenceTier.INSUFFICIENT,
    "rct": EvidenceTier.ESTABLISHED_HUMAN,
    "meta_analysis": EvidenceTier.ESTABLISHED_HUMAN,
    "systematic_review": EvidenceTier.ESTABLISHED_HUMAN,
}


@dataclasses.dataclass
class AuthResult:
    """Outcome of Claude's verification of one claim-PMID pair."""
    pmid: str
    claim: str
    ingredient: str
    pmid_valid: bool = True
    claim_supported: str = "unsupported"
    evidence_level: str = "unknown"
    finding_type: str = "not_mentioned"
    actual_finding: str = ""
    species_or_model: str = ""
    sample_size: Optional[int] = None
    reasoning: str = ""
    authenticated_tier: EvidenceTier = EvidenceTier.NONE
    flags: list = dataclasses.field(default_factory=list)
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        """Did this claim-PMID pair pass authentication?"""
        return (
            self.pmid_valid
            and self.claim_supported in ("supported", "partial")
            and self.finding_type in ("primary", "secondary")
        )

    @property
    def verdict_short(self) -> str:
        if self.error:
            return "UNVERIFIED"
        if not self.pmid_valid:
            return "PMID INVALID"
        if self.claim_supported == "fabricated":
            return "FABRICATED"
        if not self.passed:
            return "REJECTED"
        if self.claim_supported == "partial":
            return "PARTIAL"
        return "AUTHENTICATED"


class ClaudeAuthenticator:
    """
    Verifies claim-PMID pairs using Claude. Fetches the real abstract from
    PubMed directly, then asks Claude whether the paper supports the claim.
    """

    def __init__(self, api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 pubmed: Optional[PubMedClient] = None):
        self.model = model or AUTHENTICATOR_MODEL
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None
        self.pubmed = pubmed or PubMedClient()

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise RuntimeError(
                    "anthropic SDK required. Run: pip install anthropic"
                )
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._api_key.startswith("sk-ant-"))

    def fetch_ground_truth(self, pmids: list) -> dict:
        """Fetch real abstracts from PubMed. Returns {pmid: record_dict}."""
        records = self.pubmed.fetch_full_records(pmids)
        return {r["pmid"]: r for r in records}

    def verify(self, pmid: str, claim: str, ingredient: str,
               category: str = "", ground_truth: dict = None) -> AuthResult:
        """Verify a single claim-PMID pair against PubMed ground truth."""
        result = AuthResult(pmid=pmid, claim=claim, ingredient=ingredient)

        if not self.available:
            result.error = "No Anthropic API key configured"
            result.flags.append("UNVERIFIED")
            return result

        paper = (ground_truth or {}).get(pmid)
        if not paper or not paper.get("abstract"):
            result.pmid_valid = False
            result.claim_supported = "fabricated"
            result.actual_finding = (
                "PMID did not resolve to a paper with an abstract"
            )
            result.reasoning = (
                "The retrieving system cited this PMID but PubMed returned "
                "no abstract. The PMID may be fabricated or the paper may "
                "have been retracted/removed."
            )
            result.flags.append("PMID_NOT_FOUND")
            return result

        user_prompt = (
            f"## Paper (fetched directly from PubMed — ground truth)\n"
            f"PMID: {pmid}\n"
            f"Title: {paper.get('title', 'N/A')}\n"
            f"Article types: {', '.join(paper.get('article_types', []))}\n"
            f"MeSH terms: {', '.join(paper.get('mesh_terms', []))}\n"
            f"Abstract:\n{paper['abstract']}\n\n"
            f"## Claim to verify\n"
            f"Ingredient: {ingredient}\n"
            f"Product category: {category or 'general'}\n"
            f"Claim: \"{claim}\"\n\n"
            f"Does this paper's ACTUAL FINDINGS support this claim? "
            f"Call record_verification with your assessment."
        )

        parsed = None
        last_err = None
        for attempt in range(2):  # one retry on transient failure
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=[{
                        "name": "record_verification",
                        "description": (
                            "Record the structured verification of whether the "
                            "paper supports the claim."
                        ),
                        "input_schema": VERIFICATION_SCHEMA,
                    }],
                    tool_choice={"type": "tool", "name": "record_verification"},
                    messages=[{"role": "user", "content": user_prompt}],
                )
                for block in response.content:
                    if block.type == "tool_use" and block.name == "record_verification":
                        parsed = block.input
                        break
                # A well-formed rejection MUST carry reasoning. An empty/degraded
                # response is a transient failure, NOT a confident rejection —
                # do not let it silently drop good evidence.
                if parsed and not (parsed.get("reasoning")
                                   or parsed.get("actual_finding")):
                    parsed = None
                    last_err = "Degraded response (no reasoning)"
                    continue
                if parsed is not None:
                    break
                last_err = "No tool_use block in response"
            except Exception as e:  # noqa: BLE001 — retry then surface
                last_err = f"Authenticator error: {e}"

        if parsed is None:
            # UNVERIFIED, not rejected — surfaced for human review.
            result.error = last_err or "Verification failed"
            result.flags.append("AUTH_UNVERIFIED")
            return result

        try:
            result.pmid_valid = parsed.get("pmid_valid", True)
            result.claim_supported = _clean(parsed.get("claim_supported", "unsupported"))
            result.evidence_level = _clean(parsed.get("evidence_level", "unknown"))
            result.finding_type = _clean(parsed.get("finding_type", "not_mentioned"))
            result.actual_finding = _clean(parsed.get("actual_finding", ""))
            result.species_or_model = _clean(parsed.get("species_or_model", ""))
            result.sample_size = parsed.get("sample_size")
            result.reasoning = _clean(parsed.get("reasoning", ""))
            result.flags = [_clean(x) for x in parsed.get("flags", [])]
            result.authenticated_tier = _LEVEL_TO_TIER.get(
                result.evidence_level, EvidenceTier.NONE
            )

        except Exception as e:
            result.error = f"Authenticator error: {e}"
            result.flags.append("AUTH_ERROR")

        return result

    def verify_batch(self, pairs: list, category: str = "") -> list:
        """Verify multiple claim-PMID pairs. Also detects PMID spreading.

        `pairs` is a list of dicts with keys: pmid, claim, ingredient.
        Returns list of AuthResult.
        """
        all_pmids = list({p["pmid"] for p in pairs})
        ground_truth = self.fetch_ground_truth(all_pmids)

        results = []
        pmid_claim_count: dict[str, int] = {}

        for pair in pairs:
            pmid = str(pair["pmid"])
            pmid_claim_count[pmid] = pmid_claim_count.get(pmid, 0) + 1

            result = self.verify(
                pmid=pmid,
                claim=pair["claim"],
                ingredient=pair["ingredient"],
                category=category,
                ground_truth=ground_truth,
            )
            results.append(result)

        for result in results:
            count = pmid_claim_count.get(result.pmid, 0)
            if count >= 3:
                result.flags.append(
                    f"PMID_SPREADING: cited for {count} different claims"
                )
            if count >= 2 and not result.passed:
                result.flags.append(
                    "OVER_CITATION: reused PMID that failed authentication"
                )

        return results
