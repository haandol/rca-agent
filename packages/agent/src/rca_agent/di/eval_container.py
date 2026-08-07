from __future__ import annotations

from rca_agent.di.app_container import AppContainer


class EvalAppContainer(AppContainer):
    """Application container for supplied-observation model evaluation.

    Evaluation scenarios carry incident-time observations. Agents must not
    consult current external sources and accidentally replace that historical
    evidence with present AWS state.
    """

    @property
    def scoping_mcp_clients(self):
        return []

    @property
    def evidence_mcp_clients(self):
        return []
