"""Thin OpenAI wrapper: JSON calls and a tool-calling loop.

Runs the whole agent stack on a single OpenAI key - chat (gpt-4o-mini by default) plus
embeddings (text-embedding-3-small, see app/rag/retriever.py). No Anthropic key required.

Observability: every model call is written to our `logs` table as an `llm_call` row (model, tokens,
cost, latency). When LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set, the client is swapped for
Langfuse's drop-in OpenAI client, which additionally records the full prompt and response of each
generation, grouped by our trace_id (one Langfuse trace per visitor turn) and session_id.
"""
from __future__ import annotations
import json, os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from app.config import settings
from app.observability.logger import log_event, new_trace_id, timer
from app.reliability.retry import with_retry

if settings.langfuse_enabled:
    # Langfuse reads its credentials from the process environment
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.LANGFUSE_PUBLIC_KEY)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.LANGFUSE_SECRET_KEY)
    os.environ.setdefault("LANGFUSE_HOST", settings.LANGFUSE_HOST)
    from langfuse.openai import AsyncOpenAI
else:
    from openai import AsyncOpenAI

_client = AsyncOpenAI(**settings.llm_client_kwargs)

# trace_id / session_id of the turn being handled, set once by the Orchestrator so every model call
# underneath it (including ones inside asyncio.gather) is attributed without threading ids through
# every agent signature.
_trace: ContextVar[dict[str, str | None]] = ContextVar("llm_trace", default={})


@contextmanager
def bind_trace(trace_id: str, session_id: str | None):
    token = _trace.set({"trace_id": trace_id, "session_id": session_id})
    try:
        yield
    finally:
        _trace.reset(token)


def flush() -> None:
    """Send any buffered Langfuse events. Called on app shutdown."""
    if settings.langfuse_enabled:
        from langfuse.openai import openai as lf_openai
        lf_openai.flush_langfuse()


def _tracing_kwargs(name: str) -> dict[str, Any]:
    if not settings.langfuse_enabled:
        return {}   # the plain OpenAI client rejects unknown kwargs
    ctx = _trace.get()
    return {k: v for k, v in {"name": name, "trace_id": ctx.get("trace_id"),
                              "session_id": ctx.get("session_id")}.items() if v}


async def _create(name: str, *, model: str | None = None, **kwargs):
    """One chat.completions call, retried, then logged with its token usage and cost."""
    model = model or settings.OPENAI_CHAT_MODEL

    async def _call():
        return await _client.chat.completions.create(
            model=model, **kwargs, **_tracing_kwargs(name))

    ctx = _trace.get()
    with timer() as t:
        resp = await with_retry(_call, agent="llm", error_code="LLM_CALL_FAILED")

    usage = getattr(resp, "usage", None)
    tin = getattr(usage, "prompt_tokens", 0) or 0
    tout = getattr(usage, "completion_tokens", 0) or 0
    cost = (tin * settings.OPENAI_PRICE_IN_PER_M + tout * settings.OPENAI_PRICE_OUT_PER_M) / 1_000_000
    await log_event("llm_call", trace_id=ctx.get("trace_id") or new_trace_id(),
                    session_id=ctx.get("session_id"), agent=name.split(".")[0],
                    payload={"call": name, "model": model,
                             "prompt_tokens": tin, "completion_tokens": tout,
                             "cost_usd": round(cost, 6),
                             "finish_reason": resp.choices[0].finish_reason if resp.choices else None},
                    latency_ms=t["ms"])
    return resp


async def complete_json(system: str, prompt: str, *, max_tokens: int = 1024, name: str = "llm",
                        model: str | None = None) -> dict:
    """Ask for strict JSON via response_format, parsed defensively.

    `model` overrides the chat model - the guardrail can review on a stronger one (GUARDRAIL_MODEL).
    """
    resp = await _create(
        name, model=model,
        max_completion_tokens=max_tokens,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system + "\n\nRespond with a single valid JSON object."},
            {"role": "user", "content": prompt},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def to_openai_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the neutral MCP tool schemas (name/description/input_schema) to OpenAI's function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s.get("description", ""),
                "parameters": s.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for s in schemas
    ]


async def run_with_tools(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    call_tool: Callable[[str, dict], Awaitable[dict]],
    *,
    max_tokens: int = 900,
    max_turns: int = 4,
    name: str = "llm",
) -> str:
    """Generic OpenAI tool-calling loop.

    `messages` is a plain OpenAI-style history: [{"role": "user"|"assistant", "content": str}, ...].
    `call_tool(name, args) -> dict` executes one tool call. The caller owns that function, so it is
    also the policy boundary: it can reject, correct or fill in arguments before anything reaches an
    MCP server, and return a structured error the model sees and can recover from. Returns the model's
    final text reply once it stops calling tools.
    """
    convo: list[dict[str, Any]] = [{"role": "system", "content": system}] + list(messages)

    for turn in range(max_turns):
        resp = await _create(f"{name}.turn{turn + 1}", max_completion_tokens=max_tokens,
                             temperature=0.2, tools=tools, messages=convo)
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        convo.append({
            "role": "assistant",
            "content": msg.content,
            # echoed back exactly as returned: providers may attach extra fields the next request needs
            # (Gemini's extra_content.google.thought_signature); OpenAI's tool calls have none
            "tool_calls": [tc.model_dump(exclude_none=True) for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await call_tool(tc.function.name, args)
            convo.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    return "I'm having trouble completing that right now. Could you tell me the key details directly?"
