"""
Decision layer — action selector using native tool-use (NOT structured output).

next_step() receives one Goal, memory hits, attached artifact bytes, and the
full MCP tool list. It calls the gateway with tools=mcp_tools, tool_choice="auto"
and returns exactly one of:
  - DecisionOutput(tool_call=...) when the model emits tool_calls[]
  - DecisionOutput(answer=...)    when the model replies with plain text

JSONL logging: every call appends a decision trace line to the run's log.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from schemas import DecisionOutput, Goal, MemoryItem, ToolCall

sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from client import LLM  # noqa: E402

DECISION_SYSTEM = """\
You are the Decision layer. You work on ONE bounded goal.

Rule 1 — Exactly one output: either call a tool OR give a final answer. Never both.
Rule 2 — Artifact handles: strings beginning with art: are internal identifiers. \
They are NOT file paths or URLs. Never pass art: values to read_file or fetch_url — \
these calls will always fail. WRONG: read_file(path="art:09ff..."). \
When artifact bytes are needed, they appear under ATTACHED ARTIFACTS in the user message. \
If you need an artifact that is not yet attached, give a FINAL ANSWER asking Perception \
to attach it — do not call read_file or fetch_url with an art: handle.
Rule 3 — Substantive answers: when the goal asks for extraction, a list, a comparison, \
or a selection, the answer must be substantive (≥3 sentences or a list of items). \
Never return a meta-answer — provide the actual information.
Rule 4 — Be decisive: if HISTORY already contains search results, fetched pages, or \
prior answers that cover the current goal topic, give a FINAL ANSWER immediately using \
that information. Do NOT search again for the same topic. Repeated searches for the same \
query waste iterations — synthesise from what you have.
Rule 5 — Fetching from search results: When the ATTACHED ARTIFACT is a JSON array of \
web search results (objects with "url", "title", "snippet" fields) and the goal is to \
read/fetch those pages, do NOT call list_dir, read_file, or web_search. Instead: \
(a) parse the JSON array to get all URLs, \
(b) check HISTORY for any prior fetch_url calls to see which URLs are already fetched, \
(c) call fetch_url on the FIRST URL from the array that has NOT yet been fetched in HISTORY. \
Repeat in subsequent iterations for remaining URLs.
Rule 6 — Search cap: if HISTORY already contains 2 or more web_search tool calls \
related to the current goal, STOP searching and give a FINAL ANSWER immediately. \
Use the snippets and previews from those search results — they contain enough information. \
Never perform a 3rd web_search for the same topic.
Rule 7 — Missing search results: if the current goal is "Fetch Nth search result" but \
the attached search artifact contains fewer than N results (or no JSON array at all), \
give a FINAL ANSWER immediately stating that result is unavailable. Do not call any tools. \
Example: goal is "Fetch 2nd search result" but artifact has only 1 URL → answer "2nd result \
not available" and stop."""


def mcp_tools_for_decision(mcp_tools) -> list[dict]:
    """Convert MCP tool objects to gateway ToolDef dicts."""
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema or {"type": "object", "properties": {}},
        }
        for t in mcp_tools
    ]


def next_step(
    goal: Goal,
    hits: list[MemoryItem],
    attached: list[tuple[str, bytes]],
    history: list[dict],
    mcp_tools: list[dict],
    *,
    jsonl_path: Path | None = None,
    iteration: int = 0,
) -> DecisionOutput:
    """Pick the next action for a single goal."""

    # Build user message
    parts: list[str] = [f"CURRENT GOAL: {goal.text}"]

    if hits:
        mem_lines = "\n".join(
            f"  [{h.kind}] {h.descriptor}\n    value_preview: {json.dumps(h.value)[:200]}"
            for h in hits[:6]
        )
        parts.append(f"MEMORY CONTEXT:\n{mem_lines}")

    if history:
        recent = history[-5:]
        h_lines = "\n".join(
            f"  iter={e.get('iter')} kind={e.get('kind')} "
            + (f"tool={e.get('tool')} result_preview={str(e.get('result_descriptor',''))[:100]}"
               if e.get("kind") == "action"
               else f"text_preview={str(e.get('text',''))[:120]}")
            for e in recent
        )
        parts.append(f"HISTORY (recent {len(recent)}):\n{h_lines}")

    if attached:
        art_parts: list[str] = []
        for art_id, blob in attached:
            try:
                content = blob.decode("utf-8", errors="replace")
            except Exception:
                content = "[binary content]"
            if len(content) > 2500:
                content = content[:2500] + "\n...[truncated]"
            art_parts.append(f"--- {art_id} ---\n{content}")
        parts.append("ATTACHED ARTIFACTS:\n" + "\n\n".join(art_parts))

    user_msg = "\n\n".join(parts)

    llm = LLM()
    reply = llm.chat(
        messages=[{"role": "user", "content": user_msg}],
        system=DECISION_SYSTEM,
        tools=mcp_tools,
        tool_choice="auto",
        auto_route="decision",
        temperature=1.0,
        max_tokens=2048,
    )

    tool_calls = reply.get("tool_calls") or []
    if tool_calls:
        tc = tool_calls[0]
        out = DecisionOutput(
            tool_call=ToolCall(
                name=tc["name"],
                arguments=tc.get("arguments") or {},
            )
        )
    else:
        out = DecisionOutput(answer=(reply.get("text") or "").strip())

    # JSONL trace logging
    if jsonl_path is not None:
        _append_jsonl(jsonl_path, {
            "type": "decision",
            "iteration": iteration,
            "system_prompt": DECISION_SYSTEM,
            "goal": goal.text,
            "prompt": user_msg,
            "raw_response": {
                "text": reply.get("text"),
                "tool_calls": reply.get("tool_calls"),
                "provider": reply.get("provider"),
                "model": reply.get("model"),
            },
            "output": out.model_dump(),
        })

    return out


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
