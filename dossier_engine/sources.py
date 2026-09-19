"""
sources.py
==========
Thin clients over PUBLIC, citable biomedical APIs. Every method returns either
real records or an empty result. None of them ever synthesizes a plausible-
looking answer when the upstream service has nothing — that is the whole point.

Endpoints used (all public, no key required for light use):
  - PubMed E-utilities      https://eutils.ncbi.nlm.nih.gov/entrez/eutils/
  - ChEMBL REST API         https://www.ebi.ac.uk/chembl/api/data/
  - ClinicalTrials.gov v2   https://clinicaltrials.gov/api/v2/

NOTE ON ENVIRONMENT: in some sandboxes outbound access to these hosts is
blocked. The clients are written so that a network failure degrades to an
explicit `SourceUnavailable` result — which the graph treats exactly like
"no data found" (i.e. it produces an honest gap, never a guess). If you run
this where the Anthropic MCP connectors for PubMed/ChEMBL/ClinicalTrials are
available, set use_mcp=True and wire `mcp_call` to your connector bridge.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import dataclasses
from typing import Optional, Callable

from .guardrails import Citation


PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CHEMBL_BASE = "https://www.ebi.ac.uk/chembl/api/data"
CTGOV_BASE = "https://clinicaltrials.gov/api/v2"
PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_SDQ = "https://pubchem.ncbi.nlm.nih.gov/sdq/sdqagent.cgi"

USER_AGENT = "honest-dossier-engine/1.0 (research; contact: your-email@example.com)"


@dataclasses.dataclass
class SourceResult:
    ok: bool
    records: list
    source: str
    error: Optional[str] = None

    @classmethod
    def empty(cls, source: str) -> "SourceResult":
        return cls(ok=True, records=[], source=source)

    @classmethod
    def unavailable(cls, source: str, error: str) -> "SourceResult":
        # ok=False signals the GAP came from a tool failure, not from a clean
        # "searched and found nothing". The graph reports this distinction.
        return cls(ok=False, records=[], source=source, error=error)


def _get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


class PubMedClient:
    """
    Search + metadata over PubMed. Returns the article_types / mesh_terms that
    guardrails.grade_pubmed_hits() needs to assign an evidence tier honestly.

    If `mcp_call` is provided (a callable bridging to the Anthropic PubMed MCP
    connector), it is used instead of raw E-utilities. The callable should take
    (tool_name, arguments) and return the parsed tool result dict.
    """

    def __init__(self, mcp_call: Optional[Callable] = None,
                 hydrate_full: bool = False):
        self.mcp_call = mcp_call
        # When True, esearch results are hydrated via efetch so records carry
        # MeSH terms + abstracts (needed by the relevance gate on the live
        # path). esummary alone returns no MeSH, which would leave every paper
        # AMBIGUOUS. Costs one extra efetch call per search.
        self.hydrate_full = hydrate_full

    def search(self, query: str, max_results: int = 8) -> SourceResult:
        if self.mcp_call:
            try:
                res = self.mcp_call("PubMed:search_articles",
                                    {"query": query, "max_results": max_results})
                pmids = res.get("pmids", [])
                if not pmids:
                    return SourceResult.empty("PubMed")
                return self._hydrate_mcp(pmids)
            except Exception as e:  # noqa: BLE001
                return SourceResult.unavailable("PubMed", str(e))
        # Raw E-utilities path
        try:
            q = urllib.parse.quote(query)
            url = (f"{PUBMED_BASE}/esearch.fcgi?db=pubmed&retmode=json"
                   f"&retmax={max_results}&term={q}")
            data = _get_json(url)
            pmids = data.get("esearchresult", {}).get("idlist", [])
            if not pmids:
                return SourceResult.empty("PubMed")
            if self.hydrate_full:
                records = self.fetch_full_records(pmids)
                if records:
                    return SourceResult(ok=True, records=records, source="PubMed")
                # efetch failed — fall back to lighter esummary metadata.
            return self._hydrate_eutils(pmids)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return SourceResult.unavailable("PubMed", str(e))

    def _hydrate_mcp(self, pmids: list) -> SourceResult:
        res = self.mcp_call("PubMed:get_article_metadata", {"pmids": pmids})
        records = []
        for a in res.get("articles", []):
            records.append({
                "pmid": a.get("identifiers", {}).get("pmid"),
                "doi": a.get("identifiers", {}).get("doi"),
                "title": a.get("title"),
                "article_types": a.get("article_types", []),
                "mesh_terms": a.get("mesh_terms", []),
                "abstract": a.get("abstract"),
            })
        return SourceResult(ok=True, records=records, source="PubMed")

    def _hydrate_eutils(self, pmids: list) -> SourceResult:
        time.sleep(0.34)  # be polite to NCBI rate limits
        ids = ",".join(pmids)
        url = f"{PUBMED_BASE}/esummary.fcgi?db=pubmed&retmode=json&id={ids}"
        data = _get_json(url)
        result = data.get("result", {})
        records = []
        for pmid in pmids:
            item = result.get(pmid, {})
            if not item:
                continue
            records.append({
                "pmid": pmid,
                "doi": next((x.get("value") for x in item.get("articleids", [])
                             if x.get("idtype") == "doi"), None),
                "title": item.get("title"),
                "article_types": item.get("pubtype", []),
                "mesh_terms": [],  # esummary doesn't include MeSH; efetch would
                "abstract": None,
            })
        return SourceResult(ok=True, records=records, source="PubMed")

    def fetch_full_records(self, pmids: list) -> list:
        """Fetch full records with abstracts and MeSH terms via efetch XML.
        Returns list of dicts with keys: pmid, title, abstract, article_types,
        mesh_terms, doi. This fetches from PubMed directly — the ground truth,
        not whatever the retrieving LLM claims the paper says."""
        if not pmids:
            return []
        if self.mcp_call:
            try:
                res = self.mcp_call("PubMed:get_article_metadata", {"pmids": pmids})
                records = []
                for a in res.get("articles", []):
                    records.append({
                        "pmid": str(a.get("identifiers", {}).get("pmid", "")),
                        "doi": a.get("identifiers", {}).get("doi"),
                        "title": a.get("title", ""),
                        "abstract": a.get("abstract", ""),
                        "article_types": a.get("article_types", []),
                        "mesh_terms": a.get("mesh_terms", []),
                    })
                return records
            except Exception:
                return []
        try:
            time.sleep(0.34)
            ids = ",".join(str(p) for p in pmids)
            url = (f"{PUBMED_BASE}/efetch.fcgi?db=pubmed&retmode=xml&id={ids}")
            xml_text = _get_text(url, timeout=30)
            return self._parse_efetch_xml(xml_text)
        except (urllib.error.URLError, TimeoutError, OSError):
            return []

    @staticmethod
    def _parse_efetch_xml(xml_text: str) -> list:
        records = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        for article in root.findall(".//PubmedArticle"):
            citation = article.find("MedlineCitation")
            if citation is None:
                continue
            pmid_el = citation.find("PMID")
            pmid = pmid_el.text if pmid_el is not None else ""
            art = citation.find("Article")
            title = ""
            abstract = ""
            article_types = []
            if art is not None:
                title_el = art.find("ArticleTitle")
                title = title_el.text or "" if title_el is not None else ""
                abs_el = art.find("Abstract")
                if abs_el is not None:
                    parts = []
                    for at in abs_el.findall("AbstractText"):
                        label = at.get("Label", "")
                        text = "".join(at.itertext()).strip()
                        if label:
                            parts.append(f"{label}: {text}")
                        else:
                            parts.append(text)
                    abstract = "\n".join(parts)
                for pt in art.findall(".//PublicationType"):
                    if pt.text:
                        article_types.append(pt.text)
            mesh_terms = []
            mesh_list = citation.find("MeshHeadingList")
            if mesh_list is not None:
                for heading in mesh_list.findall("MeshHeading"):
                    desc = heading.find("DescriptorName")
                    if desc is not None and desc.text:
                        mesh_terms.append(desc.text)
            doi = None
            pub_data = article.find("PubmedData")
            if pub_data is not None:
                for aid in pub_data.findall(".//ArticleId"):
                    if aid.get("IdType") == "doi" and aid.text:
                        doi = aid.text
                        break
            records.append({
                "pmid": pmid, "title": title, "abstract": abstract,
                "article_types": article_types, "mesh_terms": mesh_terms,
                "doi": doi,
            })
        return records

    @staticmethod
    def to_citations(result: SourceResult, limit: int = 4) -> list:
        cites = []
        for r in result.records[:limit]:
            study_type = None
            for t in r.get("article_types", []):
                if t.lower() in ("clinical trial", "meta-analysis",
                                 "systematic review", "review",
                                 "randomized controlled trial"):
                    study_type = t
                    break
            cites.append(Citation(
                source="PubMed", identifier=str(r.get("pmid")),
                title=r.get("title"), doi=r.get("doi"), study_type=study_type,
            ))
        return cites


class ChemblClient:
    """Look up a compound and report whether it is a *known reference compound*.
    Used ONLY to attach a recognized marker compound to an ingredient — never to
    assign a concentration the product was not actually assayed for."""

    def __init__(self, mcp_call: Optional[Callable] = None):
        self.mcp_call = mcp_call

    def lookup(self, name: str) -> SourceResult:
        if self.mcp_call:
            try:
                res = self.mcp_call("ChEMBL:compound_search", {"query": name})
                recs = res if isinstance(res, list) else res.get("results", [])
                return SourceResult(ok=True, records=recs or [], source="ChEMBL")
            except Exception as e:  # noqa: BLE001
                return SourceResult.unavailable("ChEMBL", str(e))
        try:
            q = urllib.parse.quote(name)
            url = (f"{CHEMBL_BASE}/molecule/search.json?q={q}&limit=3")
            data = _get_json(url)
            molecules = data.get("molecules", [])
            return SourceResult(ok=True, records=molecules, source="ChEMBL")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return SourceResult.unavailable("ChEMBL", str(e))


class ClinicalTrialsClient:
    def __init__(self, mcp_call: Optional[Callable] = None):
        self.mcp_call = mcp_call

    def search(self, condition: str, intervention: str) -> SourceResult:
        if self.mcp_call:
            try:
                res = self.mcp_call("Clinical Trials:search_trials",
                                    {"condition": condition,
                                     "intervention": intervention})
                recs = res if isinstance(res, list) else res.get("studies", [])
                return SourceResult(ok=True, records=recs or [], source="ClinicalTrials.gov")
            except Exception as e:  # noqa: BLE001
                return SourceResult.unavailable("ClinicalTrials.gov", str(e))
        try:
            params = urllib.parse.urlencode({
                "query.cond": condition,
                "query.intr": intervention,
                "pageSize": 5,
            })
            url = f"{CTGOV_BASE}/studies?{params}"
            data = _get_json(url)
            return SourceResult(ok=True, records=data.get("studies", []),
                                source="ClinicalTrials.gov")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return SourceResult.unavailable("ClinicalTrials.gov", str(e))


class PubChemClient:
    """
    Keyless client over PubChem PUG-REST + the pathway SDQ endpoint. Used to
    resolve an ingredient's marker COMPOUND to a CID, read its identity, and
    list the curated pathways PubChem associates with it (WikiPathways,
    Reactome, PathBank, ...).

    Those pathways are treated as CANDIDATE mechanisms only — the pipeline
    still has to find literature and pass Claude authentication before any of
    them is asserted. PubChem is a compound database, so botanical names do not
    resolve; callers pass the active/marker compound name.
    """

    def __init__(self):
        pass

    def resolve_cid(self, compound_name: str) -> Optional[int]:
        if not compound_name:
            return None
        try:
            q = urllib.parse.quote(compound_name)
            url = f"{PUBCHEM_BASE}/compound/name/{q}/cids/JSON"
            data = _get_json(url)
            cids = data.get("IdentifierList", {}).get("CID", [])
            return int(cids[0]) if cids else None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def properties(self, cid: int) -> dict:
        try:
            url = (f"{PUBCHEM_BASE}/compound/cid/{cid}/property/"
                   f"MolecularFormula,MolecularWeight,IUPACName/JSON")
            data = _get_json(url)
            props = data.get("PropertyTable", {}).get("Properties", [{}])
            return props[0] if props else {}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {}

    def pathways(self, cid: int, limit: int = 20) -> SourceResult:
        """Return curated pathways associated with the CID via the SDQ agent.
        Each record: {source, name, extid}. Degrades to unavailable on error."""
        query = {
            "select": ["pwacc", "name", "source", "externalid"],
            "collection": "pathway",
            "where": {"ands": [{"cid": str(cid)}]},
            "start": 1,
            "limit": limit,
        }
        try:
            url = (f"{PUBCHEM_SDQ}?infmt=json&outfmt=json&query="
                   + urllib.parse.quote(json.dumps(query)))
            data = _get_json(url, timeout=30)
            out = data.get("SDQOutputSet", [{}])
            rows = out[0].get("rows", []) if out else []
            recs = [
                {"source": r.get("source", ""),
                 "name": r.get("name", ""),
                 "extid": r.get("externalid", "")}
                for r in rows if r.get("name")
            ]
            return SourceResult(ok=True, records=recs, source="PubChem")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            return SourceResult.unavailable("PubChem", str(e))
