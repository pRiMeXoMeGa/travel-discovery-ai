"""Per-request observability: token usage, latency, and agent step trace.

Brief §2.4 requires these exposed via an endpoint or log. A RequestTrace is
created per concierge call, agents append steps + token usage, and it is both
streamed to the client (visible agent steps) and logged.
"""
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentStep:
    agent: str
    status: str  # "start" | "done" | "error"
    detail: str | None = None
    data: Any | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


@dataclass
class RequestTrace:
    request_id: str
    query: str
    started_at: float = field(default_factory=time.perf_counter)
    steps: list[AgentStep] = field(default_factory=list)

    def add(self, step: AgentStep) -> AgentStep:
        self.steps.append(step)
        return step

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.steps)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.steps)

    @property
    def total_latency_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000

    def summary(self) -> dict:
        return {
            "request_id": self.request_id,
            "query": self.query,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "latency_ms": round(self.total_latency_ms, 1),
            "steps": [s.agent + ":" + s.status for s in self.steps],
        }
