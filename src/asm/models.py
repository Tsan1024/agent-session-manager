from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckpointPayload:
    summary: str
    completed: list[str]
    blockers: list[str]
    next_actions: list[str]
    title: str | None = None
    goal: str | None = None
