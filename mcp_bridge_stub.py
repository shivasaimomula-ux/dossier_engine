"""
mcp_bridge_stub.py
==================
The clients accept an optional `mcp_call(tool_name, arguments) -> dict`. Wire it
to whatever bridge exposes your Anthropic MCP connectors (PubMed, ChEMBL,
Clinical Trials), then pass it to the Orchestrator for a live run with real
citations.

Example shape (pseudo — replace `your_mcp_invoke` with your actual bridge):

    from dossier_engine import Orchestrator, compile_dossier
    from dossier_engine.example_run import sample_formulation

    def mcp_call(tool_name, arguments):
        # tool_name e.g. "PubMed:search_articles"
        return your_mcp_invoke(tool_name, arguments)

    orch = Orchestrator(mcp_call=mcp_call)
    state = orch.run(sample_formulation())
    print(compile_dossier(state))

The PubMed metadata tool legally requires attribution to PubMed and inclusion
of DOI links whenever you present its findings — the compiler footer reminds
you, and you should keep that attribution in any downstream rendering.
"""
