"""MCP client: connects to both servers and hands their tool schemas to the model (FR-3.3)."""
from __future__ import annotations
import asyncio, json
from typing import Any

from fastmcp import Client

from app.config import settings
from app.observability.logger import log_event, timer
from app.reliability.retry import ToolFailure, with_retry
from app.contracts import AgentError

TOOL_SERVER = {
    "calendar_health": "calendar",
    "check_availability": "calendar", "propose_slots": "calendar", "create_event": "calendar",
    "modify_event": "calendar", "cancel_event": "calendar",
    "send_lead_summary": "email", "update_lead_summary": "email",
}


class MCPHub:
    def __init__(self) -> None:
        self._schemas: list[dict[str, Any]] = []
        self.urls = {"calendar": settings.MCP_CALENDAR_URL, "email": settings.MCP_EMAIL_URL}

    async def load_schemas(self, *, retries: int = 5, delay: float = 2.0) -> list[dict[str, Any]]:
        """Load tool schemas from both MCP servers.

        Retries with a fixed delay: at boot, the API is often started a moment before the MCP
        servers finish coming up, and a single failed attempt used to leave `mcp_tools` empty for
        the life of the process. Missing servers are logged and skipped - one dead server never
        blocks the other's tools from loading.
        """
        schemas: list[dict[str, Any]] = []
        for name, url in self.urls.items():
            last_exc: Exception | None = None
            for attempt in range(1, retries + 1):
                try:
                    async with Client(url) as client:
                        for tool in await client.list_tools():
                            schemas.append({
                                "name": tool.name,
                                "description": (tool.description or "").strip(),
                                "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
                            })
                    last_exc = None
                    break
                except Exception as exc:  # server not up yet, or genuinely down
                    last_exc = exc
                    if attempt < retries:
                        await asyncio.sleep(delay)
            if last_exc is not None:
                await log_event("error", trace_id="00000000-0000-0000-0000-000000000000",
                                agent="mcp_client",
                                payload={"server": name, "url": url,
                                         "detail": f"schema load failed after {retries} attempts: {last_exc}"})
        self._schemas = schemas
        return schemas

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def schemas_for(self, names: list[str]) -> list[dict[str, Any]]:
        return [s for s in self._schemas if s["name"] in names]

    async def call(self, tool: str, args: dict, *, trace_id: str, session_id: str | None = None,
                   agent: str = "orchestrator") -> dict:
        server = TOOL_SERVER.get(tool)
        if server is None:
            raise ToolFailure(AgentError(error_code="UNKNOWN_TOOL", message=tool,
                                         retryable=False, agent=agent))
        url = self.urls[server]

        async def _invoke():
            async with Client(url) as client:
                res = await client.call_tool(tool, args)
                # fastmcp returns a list of content blocks (older) or an object with .content (newer)
                blocks = res if isinstance(res, list) else getattr(res, "content", None) or []
                payload = getattr(blocks[0], "text", "{}") if blocks else "{}"
                data = json.loads(payload) if isinstance(payload, str) else payload
                # a structured error from the tool is honoured, not swallowed (FR-8.3)
                if isinstance(data, dict) and data.get("status") == "error":
                    raise ToolFailure(AgentError(
                        error_code=data.get("error_code", "TOOL_ERROR"),
                        message=data.get("message", ""),
                        retryable=bool(data.get("retryable", False)),
                        agent=data.get("agent", server + "_mcp"),
                    ))
                return data

        with timer() as t:
            try:
                result = await with_retry(_invoke, agent=agent, error_code="TOOL_FAILED",
                                          trace_id=trace_id, session_id=session_id, tool=tool)
            except ToolFailure as tf:
                await log_event("tool_call", trace_id=trace_id, session_id=session_id, agent=agent,
                                payload={"tool": tool, "args": args, "error": tf.error.model_dump()},
                                latency_ms=t["ms"])
                raise
        await log_event("tool_call", trace_id=trace_id, session_id=session_id, agent=agent,
                        payload={"tool": tool, "args": args, "result": result}, latency_ms=t["ms"])
        return result


hub = MCPHub()
