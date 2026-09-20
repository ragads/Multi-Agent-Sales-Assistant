"""Thin OpenAI wrapper: JSON calls and a tool-calling loop.

Runs the whole agent stack on a single OpenAI key - chat (gpt-4o-mini by default) plus
embeddings (text-embedding-3-small, see app/rag/retriever.py). No Anthropic key required.
"""
from __future__ import annotations
import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from app.config import settings
from app.reliability.retry import with_retry

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def complete_json(system: str, prompt: str, *, max_tokens: int = 1024,
                        model: str | None = None) -> dict:
    """Ask for strict JSON via response_format, parsed defensively.

    `model` overrides the default chat model - the guardrail uses it to review on a stronger one.
    """
    async def _call():
        return await _client.chat.completions.create(
            model=model or settings.OPENAI_CHAT_MODEL,
            max_completion_tokens=max_tokens,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system + "\n\nRespond with a single valid JSON object."},
                {"role": "user", "content": prompt},
            ],
        )

    resp = await with_retry(_call, agent="llm", error_code="LLM_CALL_FAILED")
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
) -> str:
    """Generic OpenAI tool-calling loop.

    `messages` is a plain OpenAI-style history: [{"role": "user"|"assistant", "content": str}, ...].
    `call_tool(name, args) -> dict` executes one tool call (the caller decides what that does - in this
    project it's always app.mcp_client.hub.call, so every tool invocation is a real MCP call, never a
    stub). Returns the model's final text reply once it stops calling tools.
    """
    convo: list[dict[str, Any]] = [{"role": "system", "content": system}] + list(messages)

    for _ in range(max_turns):
        async def _call():
            return await _client.chat.completions.create(
                model=settings.OPENAI_CHAT_MODEL,
                max_completion_tokens=max_tokens,
                temperature=0.2,
                tools=tools,
                messages=convo,
            )

        resp = await with_retry(_call, agent="llm", error_code="LLM_CALL_FAILED")
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return msg.content or ""

        convo.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
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
