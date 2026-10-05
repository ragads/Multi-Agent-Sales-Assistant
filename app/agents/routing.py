"""The Orchestrator's routing policy as a pure function (FR-3.5, FR-3.6).

No I/O: given the classifier's ranked intents, pick which route a turn takes. Both orchestration
engines use it - the native pipeline calls it directly, and the LangGraph graph (app/agents/graph.py)
uses it in its `decide` node, whose result drives the graph's conditional edges. Keeping it pure means
the policy is unit-tested offline and is identical in both engines.
"""
from __future__ import annotations
from typing import Optional

ACTIONABLE = {"search", "schedule", "reschedule", "cancel"}
SCHEDULING = {"schedule", "reschedule", "cancel"}

# every route the graph can take after `decide`; each is a node in app/agents/graph.py
ROUTES = ("clarify", "close", "ack", "smalltalk", "sequence", "conflict", "parallel", "search", "scheduler")


def decide_route(intents: list[dict], top_conf: float, floor: Optional[float] = None) -> dict:
    """Return {"route": <one of ROUTES>, "decision": <routing-log skeleton>}.

    floor: the intent-confidence floor (FR-3.6); defaults to settings.INTENT_CONFIDENCE_FLOOR.
    """
    if floor is None:
        from app.config import settings
        floor = settings.INTENT_CONFIDENCE_FLOOR

    names = [i["intent"] for i in intents]
    actionable = [n for n in names if n in ACTIONABLE]
    decision = {"chosen_agents": [], "reason": "", "multi_intent_policy": None,
                "end_conversation": False, "confidence": top_conf}

    if actionable and top_conf < floor:
        route = "clarify"                                   # FR-3.6: ask rather than misroute
    elif "end_conversation" in names and not actionable:
        route = "close"
    elif not actionable:
        route = "ack" if "provide_info" in names else "smalltalk"
    else:
        sched = [n for n in actionable if n in SCHEDULING]
        has_search = "search" in actionable
        if has_search and sched:
            route = "sequence"                              # question + booking (FR-3.5)
        elif len(sched) > 1:
            route = "conflict"                              # e.g. book AND cancel: ask which
        elif actionable.count("search") > 1:
            route = "parallel"                              # independent questions
        elif has_search:
            route = "search"
        else:
            route = "scheduler"
    return {"route": route, "decision": decision}
