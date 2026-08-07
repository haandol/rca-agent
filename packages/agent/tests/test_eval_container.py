from rca_agent.di.eval_container import EvalAppContainer


def test_eval_container_disables_external_scoping_and_evidence_mcp_clients() -> None:
    container = EvalAppContainer("")

    assert container.scoping_mcp_clients == []
    assert container.evidence_mcp_clients == []
