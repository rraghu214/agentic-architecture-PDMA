"""
Memory layer — typed, persistent, keyword-indexed store.

read()          — keyword overlap search, NO LLM
filter()        — structured filter, NO LLM
relevant()      — LLM-ranked recall (auto_route="memory")
remember()      — LLM classification + append (auto_route="memory", provider="g")
record_outcome()— typed tool-outcome append, NO LLM
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from schemas import MemoryClassification, MemoryItem, ToolCall

# ── Gateway import (llm_gatewayV3 is at project root) ───────────────────────
sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from client import LLM  # noqa: E402

# ── Persistence ──────────────────────────────────────────────────────────────

MEMORY_FILE = Path("state/memory.json")


def _load() -> list[MemoryItem]:
    if not MEMORY_FILE.exists():
        return []
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return [MemoryItem.model_validate(d) for d in data]
    except Exception:
        return []


def _save(items: list[MemoryItem]) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        json.dumps([json.loads(i.model_dump_json()) for i in items], indent=2),
        encoding="utf-8",
    )


# ── Public API ───────────────────────────────────────────────────────────────

def read(
    query: str,
    history: list[dict],
    kinds: list[str] | None = None,
    top_k: int = 8,
) -> list[MemoryItem]:
    """Keyword-overlap search — no LLM call."""
    items = _load()

    # Build query token set from the query string + last 3 history descriptors
    query_words: set[str] = set(query.lower().split())
    for entry in history[-3:]:
        for field in ("result_descriptor", "text", "tool"):
            if val := entry.get(field):
                query_words.update(str(val).lower().split())

    if kinds:
        items = [i for i in items if i.kind in kinds]

    def score(item: MemoryItem) -> tuple[int, datetime]:
        kw_set = set(k.lower() for k in item.keywords)
        overlap = len(kw_set & query_words)
        return (overlap, item.created_at)

    scored = sorted(items, key=score, reverse=True)

    # Fallback: if no overlaps at all, return most-recent top_k
    if scored and score(scored[0])[0] == 0:
        return sorted(items, key=lambda i: i.created_at, reverse=True)[:top_k]

    return scored[:top_k]


def filter(
    kinds: list[str] | None = None,
    goal_id: str | None = None,
    recent: int | None = None,
) -> list[MemoryItem]:
    """Structured filter — no LLM call."""
    items = _load()
    if kinds:
        items = [i for i in items if i.kind in kinds]
    if goal_id:
        items = [i for i in items if i.goal_id == goal_id]
    items = sorted(items, key=lambda i: i.created_at, reverse=True)
    if recent is not None:
        items = items[:recent]
    return items


def relevant(
    query: str,
    kinds: list[str] | None = None,
    top_k: int = 5,
) -> list[MemoryItem]:
    """LLM-ranked recall — one gateway call (auto_route='memory')."""
    items = _load()
    if kinds:
        items = [i for i in items if i.kind in kinds]
    if not items:
        return []

    summaries = "\n".join(
        f"[{i}] {item.descriptor} | keywords: {', '.join(item.keywords)}"
        for i, item in enumerate(items)
    )
    llm = LLM()
    reply = llm.chat(
        prompt=(
            f"Query: {query}\n\nMemory items:\n{summaries}\n\n"
            f"Return the indices (0-based) of the top {top_k} most relevant items "
            f"as a JSON array of integers, e.g. [0, 3, 1]."
        ),
        auto_route="memory",
        temperature=0,
        max_tokens=128,
    )
    try:
        text = (reply.get("text") or "").strip()
        start = text.find("[")
        end = text.rfind("]") + 1
        indices = json.loads(text[start:end])
        return [items[i] for i in indices if 0 <= i < len(items)][:top_k]
    except Exception:
        return items[:top_k]


def remember(
    raw_text: str,
    source: str,
    run_id: str,
    goal_id: str = "",
) -> MemoryItem:
    """Classify and store a memory item — one LLM call (auto_route='memory', provider='g')."""
    llm = LLM()
    reply = llm.chat(
        prompt=f"Classify this text into a structured memory item. Return JSON only.\n\nText: {raw_text}",
        system=(
            'Return ONLY a JSON object with these exact fields:\n'
            '{\n'
            '  "kind": "fact"|"preference"|"tool_outcome"|"scratchpad",\n'
            '  "keywords": ["3 to 8 lowercase tokens"],\n'
            '  "descriptor": "one short human-readable line",\n'
            '  "value": {"key": "structured payload relevant to the text"},\n'
            '  "confidence": 0.0\n'
            '}\n'
            'No prose, no markdown fences. Only the JSON object.'
        ),
        auto_route="memory",
        temperature=0,
        max_tokens=512,
    )

    # Parse JSON from text — more robust than response_format for open dict fields
    try:
        text = (reply.get("text") or "{}").strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        start = text.find("{")
        end = text.rfind("}") + 1
        mc = MemoryClassification.model_validate_json(text[start:end])
    except Exception:
        mc = MemoryClassification(
            kind="scratchpad",
            keywords=raw_text.lower().split()[:5],
            descriptor=raw_text[:80],
            value={"raw": raw_text[:200]},
            confidence=0.5,
        )

    item = MemoryItem(
        id=uuid.uuid4().hex[:12],
        kind=mc.kind,
        keywords=mc.keywords,
        descriptor=mc.descriptor,
        value=mc.value,
        artifact_id=None,
        source=source,
        run_id=run_id,
        goal_id=goal_id or None,
        confidence=mc.confidence,
        created_at=datetime.now(timezone.utc),
    )

    items = _load()
    items.append(item)
    _save(items)
    return item


def record_outcome(
    tool_call: ToolCall,
    result_text: str,
    artifact_id: str | None,
    run_id: str,
    goal_id: str,
) -> None:
    """Store a tool_outcome memory entry — no LLM call."""
    # keywords = tool name + first 3 string argument values
    kw_extras = [
        str(v)[:30]
        for v in list(tool_call.arguments.values())[:3]
        if isinstance(v, str)
    ]
    keywords = [tool_call.name] + kw_extras

    item = MemoryItem(
        id=uuid.uuid4().hex[:12],
        kind="tool_outcome",
        keywords=keywords,
        descriptor=f"{tool_call.name} → {result_text[:80]}",
        value={
            "tool": tool_call.name,
            "args": tool_call.arguments,
            "result": result_text[:500],
        },
        artifact_id=artifact_id,
        source="action",
        run_id=run_id,
        goal_id=goal_id or None,
        confidence=1.0,
        created_at=datetime.now(timezone.utc),
    )

    items = _load()
    items.append(item)
    _save(items)
