"""Shared contracts. Every agent speaks exactly this language (FR-3.1, FR-8.3)."""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class SessionState(BaseModel):
    id: str
    visitor_key: str
    status: str = "active"
    version: int = 0
    visitor_tz: Optional[str] = None
    qualification: dict[str, Any] = Field(default_factory=dict)
    agents_run: list[dict[str, Any]] = Field(default_factory=list)
    booking: Optional[dict[str, Any]] = None
    summary_sent: Optional[dict[str, Any]] = None
    history: list[dict[str, Any]] = Field(default_factory=list)  # [{role, content, agent}]

    def recent(self, n: int = 8) -> list[dict[str, Any]]:
        return self.history[-n:]

    def transcript(self) -> str:
        return "\n".join(f"{m['role']}: {m['content']}" for m in self.history)


class AgentRequest(BaseModel):
    session_id: str
    trace_id: str
    message: Optional[str] = None
    state: SessionState
    params: dict[str, Any] = Field(default_factory=dict)


class AgentError(BaseModel):
    """Exact shape mandated by FR-8.3."""
    status: Literal["error"] = "error"
    error_code: str
    message: str
    retryable: bool
    agent: str


class AgentResponse(BaseModel):
    status: Literal["ok", "error"] = "ok"
    agent: str
    output: dict[str, Any] = Field(default_factory=dict)
    confidence: Optional[float] = None       # FR-4.7
    citations: list[str] = Field(default_factory=list)
    state_patch: dict[str, Any] = Field(default_factory=dict)
    error: Optional[AgentError] = None

    @property
    def reply(self) -> str:
        return self.output.get("reply", "") if self.output else ""


def err(agent: str, code: str, message: str, retryable: bool) -> AgentResponse:
    return AgentResponse(
        status="error",
        agent=agent,
        error=AgentError(error_code=code, message=message, retryable=retryable, agent=agent),
    )


class GuardrailVerdict(BaseModel):
    verdict: Literal["allow", "block"]
    category: str = "none"
    reason: str = ""
    safe_fallback: Optional[str] = None
