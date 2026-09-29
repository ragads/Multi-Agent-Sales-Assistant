"""Bearer-token auth for the MCP servers.

Each MCP server gets its own shared secret (MCP_CALENDAR_TOKEN / MCP_EMAIL_TOKEN). Every HTTP request
must carry `Authorization: Bearer <token>`; anything else gets a 401 before it reaches FastMCP, so an
unauthenticated caller can neither list nor call tools. Pure ASGI middleware, which works with both
the pinned fastmcp 2.3.x and fastmcp 4.x `http_app(middleware=...)`.
"""
from __future__ import annotations
import hmac

import uvicorn
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from app.config import settings


class BearerAuthMiddleware:
    def __init__(self, app, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):   # constant time: no timing oracle
                resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def serve(mcp, *, port: int, token: str) -> None:
    """Run an MCP server over streamable HTTP at /mcp, behind bearer auth."""
    app = mcp.http_app(path="/mcp", middleware=[Middleware(BearerAuthMiddleware, token=token)])
    uvicorn.run(app, host=settings.MCP_BIND_HOST, port=port)
