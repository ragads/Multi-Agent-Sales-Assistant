"""The Orchestrator's turn as a LangGraph StateGraph (the default engine: ORCHESTRATOR_ENGINE=langgraph).

    START -> inbound --(blocked)--> END
                 \\-> classify -> decide --(route)--> clarify | close | ack | smalltalk | search
                                                      | scheduler | sequence | parallel | conflict
                                                   -> compose -> guardrail -> persist -> END

Every node runs one of the Orchestrator's step functions (app/agents/orchestrator.py), so the graph and
the native engine share a single implementation of each step. The routing policy
(app/agents/routing.py) becomes the graph's conditional edges, which makes the control flow explicit,
inspectable (`turn_graph.get_graph().draw_mermaid()`) and recorded per turn (`graph_path` in the
routing_decision log row).

What LangGraph deliberately does NOT own here (DECISIONS.md, decision 13):
- Session state and memory. They live in Supabase under row-level security, the lease lock and
  optimistic writes, so no LangGraph checkpointer is attached - a second copy of session state would
  have to be kept in sync with the sessions table the sweeper and the RLS policies depend on.
- The tool-calling loop. The Scheduler and Lead-Summary agents keep their own loop because its
  call_tool function is the policy boundary that hides and validates model-owned arguments.
"""
from __future__ import annotations
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.orchestrator import orchestrator
from app.agents.routing import ROUTES, decide_route


class TurnState(TypedDict, total=False):
    # input
    session_id: str
    message: str
    trace_id: str
    t0: float
    # filled in by the nodes
    state: Any            # SessionState
    req: Any              # AgentRequest
    cls: dict
    intents: list
    signals: dict
    top_conf: float
    route: str
    decision: dict
    responses: list       # list[AgentResponse]
    slots: list
    draft: str
    context: str
    outbound: Any         # GuardrailVerdict
    result: dict
    path: Annotated[list, operator.add]   # nodes visited, appended by every node


def _node(name: str, step):
    """Wrap an Orchestrator step as a graph node that also records itself in `path`."""
    async def run(state: TurnState) -> dict:
        update = await step(state)
        return {**update, "path": [name]}
    run.__name__ = name
    return run


async def _decide(state: TurnState) -> dict:
    return decide_route(state["intents"], state["top_conf"])


def build_turn_graph():
    g = StateGraph(TurnState)
    g.add_node("inbound", _node("inbound", orchestrator.step_inbound))
    g.add_node("classify", _node("classify", orchestrator.step_classify))
    g.add_node("decide", _node("decide", _decide))
    for route in ROUTES:
        g.add_node(route, _node(route, getattr(orchestrator, f"route_{route}")))
    g.add_node("compose", _node("compose", orchestrator.step_compose))
    g.add_node("guardrail", _node("guardrail", orchestrator.step_guardrail))
    g.add_node("persist", _node("persist", orchestrator.step_persist))

    g.add_edge(START, "inbound")
    # a message blocked by the inbound guardrail is answered in `inbound` and the turn ends
    g.add_conditional_edges("inbound", lambda s: "end" if s.get("result") else "classify",
                            {"end": END, "classify": "classify"})
    g.add_edge("classify", "decide")
    # the routing policy (FR-3.5, FR-3.6) as conditional edges
    g.add_conditional_edges("decide", lambda s: s["route"], {r: r for r in ROUTES})
    for route in ROUTES:
        g.add_edge(route, "compose")
    g.add_edge("compose", "guardrail")
    g.add_edge("guardrail", "persist")
    g.add_edge("persist", END)
    return g.compile()


turn_graph = build_turn_graph()
