"""Honest multi-agent clinical & regulatory evidence engine."""
from .state import FormulationInput, Ingredient, GraphState
from .nodes import Orchestrator
from .compiler import compile_dossier
from .compiler_v2 import compile_v2
from .guardrails import EvidenceTier, EvidenceRecord, Citation
from .authenticator import ClaudeAuthenticator, AuthResult

__all__ = [
    "FormulationInput", "Ingredient", "GraphState",
    "Orchestrator", "compile_dossier", "compile_v2",
    "EvidenceTier", "EvidenceRecord", "Citation",
    "ClaudeAuthenticator", "AuthResult",
]
