"""Putting the A2A endpoint on the web app: the public agent card, and the JSON-RPC endpoint behind token authentication."""
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from fastapi import FastAPI

from finance_agent.a2a.auth import TokenVerifier, authenticate_middleware
from finance_agent.a2a.card import build_card
from finance_agent.a2a.executor import FinanceExecutor
from finance_agent.db.session import engine
from finance_agent.llm import LLM


class _Context(DefaultServerCallContextBuilder):
    """Hands the verified token claims (set by the auth middleware) to the executor."""

    def build(self, request):
        ctx = super().build(request)
        ctx.state["claims"] = getattr(request.state, "claims", None)
        return ctx


def mount_a2a(app: FastAPI, llm: LLM, verifier: TokenVerifier | None = None) -> DatabaseTaskStore:
    """Tasks are kept in the database, so a conversation waiting for an answer survives a restart."""
    card = build_card()
    store = DatabaseTaskStore(engine)
    handler = DefaultRequestHandler(agent_executor=FinanceExecutor(llm), task_store=store, agent_card=card)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=_Context()),
    )
    app.middleware("http")(authenticate_middleware(verifier or TokenVerifier()))
    return store
