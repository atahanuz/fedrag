"""Structured trace of a run: which agents ran, which tools they called, what they found.

Listeners receive events as they happen (CLI live view, web UI streaming, JSON logs).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    t: float
    agent: str
    type: str  # plan | agent_start | tool_call | tool_result | agent_finish | synthesis | verification | final | error | info
    data: dict[str, Any] = field(default_factory=dict)


class Trace:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.listeners: list[Callable[[Event], None]] = []
        self.t0 = time.time()

    def emit(self, agent: str, type: str, **data: Any) -> Event:
        ev = Event(t=round(time.time() - self.t0, 2), agent=agent, type=type, data=data)
        self.events.append(ev)
        for fn in self.listeners:
            try:
                fn(ev)
            except Exception:  # a broken listener must never break a run
                pass
        return ev

    def to_list(self) -> list[dict]:
        return [asdict(e) for e in self.events]

    def agents_used(self) -> list[str]:
        seen: list[str] = []
        for e in self.events:
            if e.type == "agent_start" and e.agent not in seen:
                seen.append(e.agent)
        return seen

    def tool_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            if e.type == "tool_call":
                out[e.data["tool"]] = out.get(e.data["tool"], 0) + 1
        return out
