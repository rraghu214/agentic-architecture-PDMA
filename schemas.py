"""
Pydantic contracts for every role boundary in the PMDA architecture.
All schemas match the exact shapes from the Session 6 PDF.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Core domain types ────────────────────────────────────────────────────────

class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict                  # structured payload — dict, NOT str
    artifact_id: str | None      # "art:<sha256-prefix>" or None
    source: str
    run_id: str
    goal_id: str | None
    confidence: float
    created_at: datetime


class Artifact(BaseModel):
    id: str                      # "art:<sha256-prefix>"
    content_type: str
    size_bytes: int
    source: str
    descriptor: str


class Goal(BaseModel):
    id: str
    text: str
    done: bool
    attach_artifact_id: str | None = None


class Observation(BaseModel):
    goals: list[Goal]

    @property
    def all_done(self) -> bool:
        return all(g.done for g in self.goals)

    def next_unfinished(self) -> Goal | None:
        return next((g for g in self.goals if not g.done), None)


class ToolCall(BaseModel):
    name: str
    arguments: dict


class DecisionOutput(BaseModel):
    answer: str | None = None
    tool_call: ToolCall | None = None

    @property
    def is_answer(self) -> bool:
        return self.answer is not None


# ── Internal LLM schemas (not exposed to callers) ────────────────────────────

class MemoryClassification(BaseModel):
    """Schema for the LLM call inside memory.remember()."""
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]          # 3-8 lowercase tokens
    descriptor: str              # one-line human label
    value: dict                  # structured payload extracted by LLM
    confidence: float


class PerceptionGoal(BaseModel):
    """LLM-facing goal — uses artifact_index (int) instead of handle string."""
    text: str
    done: bool
    artifact_index: int | None = None   # integer index → outer loop maps to art: handle


class PerceptionObservation(BaseModel):
    """Structured output schema for perception.observe() LLM call."""
    goals: list[PerceptionGoal]
