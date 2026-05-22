"""
Perception layer — goal decomposition and tracking.

observe() is the only public function. It:
  1. On first call: decomposes the query into 1-8 ordered atomic goals.
  2. On subsequent calls: updates done flags from history, resolves artifact_index -> art: handle.
  3. Enforces sticky-done (goals never revert from done=True).
  4. Applies force-attach safety net for synthesis goals.
  5. Appends a perception trace line to the run's JSONL log.

LLM config: auto_route="perception", provider="g", temperature=1.0
             response_format=PerceptionObservation schema
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from schemas import Goal, MemoryItem, Observation, PerceptionObservation

sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from llm_gatewayV3.client import LLM  # noqa: E402

SYNTHESIS_KEYWORDS = {"synthesise", "synthesize", "extract", "list", "compare", "decide", "summarise", "summarize"}

SYSTEM_PROMPT = """\
You are the Perception layer. You maintain the goal list.

First call (no prior goals): Decompose the user query into 2-5 concrete, atomic, \
ordered sub-goals. Rules:
- NEVER put the whole query as a single goal — always split into at least 2 steps.
- Each goal must address EXACTLY ONE action. Never combine multiple actions into one \
goal using "and". BAD: "Find activities and check weather and recommend". \
GOOD: separate goals "Find activities", "Check weather", "Recommend best option".
- Each goal must be achievable in ONE tool call or one answer.
- Create "Fetch Nth result" goals ONLY when the user EXPLICITLY asks to read/visit/fetch \
individual pages (e.g. "read the top 3 results", "fetch each URL", "visit those pages"). \
For general information queries — "find activities", "search for X", "check the weather" — \
ONE search goal is sufficient; Decision uses the search snippets without fetching full pages. \
BAD for "Find 3 activities in Tokyo": "Fetch 1st result", "Fetch 2nd result", "Fetch 3rd result". \
GOOD for "Find 3 activities in Tokyo": ONE goal "Search for family-friendly activities in Tokyo". \
GOOD for "read the top 3 results": separate "Fetch 1st result", "Fetch 2nd result", "Fetch 3rd result".
- End with a synthesis/answer goal (e.g. "Summarise/compare/list from fetched pages").
Return all done=false.

Subsequent calls (prior goals provided):
1. For each prior goal, examine the run history. Mark done=true the moment history \
contains a satisfying action or answer for that goal. Once done, stays done — never flip back. \
For "Fetch Nth result" goals: mark done only when a fetch_url call for that specific result \
appears in history (1st→first fetch_url in history, 2nd→second fetch_url, etc.).
2. Do NOT reorder, insert in the middle, or drop goals.
3. For the first unfinished goal: set artifact_index when the goal needs artifact data: \
(a) synthesis/extraction goals (extract, synthesise, list, compare, decide) — attach the \
relevant artifact; (b) "Fetch Nth search result" goals — ALWAYS attach the web_search \
results artifact so Decision can parse the URLs. If no artifact needed, set to null.
4. Return the complete updated goal list in the same order.

Return ONLY a JSON object — no prose, no markdown fences. Exact format:
{
  "goals": [
    {"text": "short imperative goal", "done": false, "artifact_index": null},
    {"text": "another goal", "done": true, "artifact_index": 0}
  ]
}
artifact_index must be an integer (0-based index into INDEXED ARTIFACTS) or null."""


def observe(
    query: str,
    hits: list[MemoryItem],
    history: list[dict],
    prior_goals: list[Goal],
    run_id: str,
    *,
    jsonl_path: Path | None = None,
    iteration: int = 0,
) -> Observation:
    """Decompose or update the goal list, resolve artifact indices, enforce sticky-done."""

    # Build indexed artifact list — only artifacts from the current run to prevent
    # cross-run contamination (e.g. B's Tokyo artifacts leaking into D's asyncio goals)
    artifact_hits = [(i, h) for i, h in enumerate(hits) if h.artifact_id and h.run_id == run_id]

    artifact_index_str = ""
    if artifact_hits:
        lines = [f"[{i}] {h.artifact_id} — {h.descriptor}" for i, h in artifact_hits]
        artifact_index_str = "INDEXED ARTIFACTS:\n" + "\n".join(lines)

    # Build the user message
    parts: list[str] = [f"USER QUERY: {query}"]

    if prior_goals:
        goal_lines = "\n".join(
            f"  [{g.id}] {'[done]' if g.done else '[todo]'} {g.text}"
            for g in prior_goals
        )
        parts.append(f"PRIOR GOALS (preserve IDs and order):\n{goal_lines}")

    if history:
        recent = history[-10:]
        h_lines = "\n".join(
            f"  iter={e.get('iter')} kind={e.get('kind')} goal_id={e.get('goal_id','')} "
            + (f"tool={e.get('tool')} result_preview={str(e.get('result_descriptor',''))[:120]}"
               if e.get("kind") == "action"
               else f"ANSWER_preview={str(e.get('text',''))[:250]}")
            for e in recent
        )
        parts.append(f"RUN HISTORY (recent) — 'kind=answer' entries ARE completed answers satisfying their goal:\n{h_lines}")

    if hits:
        mem_lines = "\n".join(
            f"  [{h.kind}] {h.descriptor}"
            for h in hits[:5]
        )
        parts.append(f"RELEVANT MEMORY:\n{mem_lines}")

    if artifact_index_str:
        parts.append(artifact_index_str)

    user_msg = "\n\n".join(parts)

    llm = LLM()
    reply = llm.chat(
        messages=[{"role": "user", "content": user_msg}],
        system=SYSTEM_PROMPT,
        auto_route="perception",
        temperature=1.0,
        max_tokens=1024,
    )

    # Parse the LLM response — strip markdown fences, extract JSON
    from schemas import PerceptionGoal
    perc_obs: PerceptionObservation | None = None
    if reply.get("parsed"):
        try:
            perc_obs = PerceptionObservation.model_validate(reply["parsed"])
        except Exception:
            pass
    if perc_obs is None:
        try:
            text = (reply.get("text") or "{}").strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            start = text.find("{")
            end = text.rfind("}") + 1
            perc_obs = PerceptionObservation.model_validate_json(text[start:end])
        except Exception:
            # Last resort: keep prior goals unchanged
            perc_obs = PerceptionObservation(
                goals=[
                    PerceptionGoal(text=g.text, done=g.done, artifact_index=None)
                    for g in prior_goals
                ]
                if prior_goals
                else [PerceptionGoal(text=query, done=False, artifact_index=None)]
            )

    # Map PerceptionGoal -> Goal
    # Re-use prior IDs positionally; assign new IDs for new goals
    prior_id_map = [g.id for g in prior_goals]

    goals: list[Goal] = []
    for idx, pg in enumerate(perc_obs.goals):
        goal_id = prior_id_map[idx] if idx < len(prior_id_map) else uuid.uuid4().hex[:8]

        # Resolve artifact_index -> actual art: handle
        attach: str | None = None
        if pg.artifact_index is not None and 0 <= pg.artifact_index < len(artifact_hits):
            attach = artifact_hits[pg.artifact_index][1].artifact_id

        goals.append(Goal(id=goal_id, text=pg.text, done=pg.done, attach_artifact_id=attach))

    # Safety net: first call must produce ≥2 goals (LLM sometimes returns the whole query as 1)
    if not prior_goals and len(goals) < 2:
        goals.append(Goal(
            id=uuid.uuid4().hex[:8],
            text="Synthesize all gathered information and provide the final answer",
            done=False,
            attach_artifact_id=None,
        ))

    # Sticky-done enforcement: goals that were done in prior list stay done
    for i, g in enumerate(goals):
        if i < len(prior_goals) and prior_goals[i].done:
            g.done = True
            # Preserve prior attach if LLM lost it
            if g.attach_artifact_id is None and prior_goals[i].attach_artifact_id:
                g.attach_artifact_id = prior_goals[i].attach_artifact_id

    # Force-done safety net: if decision gave ≥2 answers for a goal, the LLM clearly
    # answered it — mark done regardless of perception's judgment
    answer_counts: dict[str, int] = {}
    for e in history:
        if e.get("kind") == "answer":
            gid = e.get("goal_id", "")
            if gid:
                answer_counts[gid] = answer_counts.get(gid, 0) + 1
    for g in goals:
        if not g.done and answer_counts.get(g.id, 0) >= 2:
            g.done = True

    # Force-attach safety net
    unfinished = next((g for g in goals if not g.done), None)
    if unfinished and artifact_hits:
        words = set(unfinished.text.lower().split())

        # Case 1: "Fetch/Read Nth search result / URL / page" goals MUST always have the
        # web_search artifact so Decision can parse URLs. Override even if Perception LLM
        # already set an artifact_index — it may have picked a fetched-page blob instead of
        # the search-results JSON, which produces an empty URL and a wasted iteration.
        fetch_trigger = {"fetch", "read", "get"} & words
        url_trigger = {"result", "url", "page", "link"} & words
        if fetch_trigger and url_trigger:
            ws_hits = [(i, h) for i, h in artifact_hits
                       if isinstance(h.value, dict) and h.value.get("tool") == "web_search"]
            if ws_hits:
                # Always override to oldest web_search artifact = primary search results
                unfinished.attach_artifact_id = ws_hits[-1][1].artifact_id

        # Case 2: synthesis goals — attach most recent artifact (only when not already set)
        elif unfinished.attach_artifact_id is None and words & SYNTHESIS_KEYWORDS:
            unfinished.attach_artifact_id = artifact_hits[-1][1].artifact_id

    obs = Observation(goals=goals)

    # JSONL trace logging
    if jsonl_path is not None:
        _append_jsonl(jsonl_path, {
            "type": "perception",
            "iteration": iteration,
            "system_prompt": SYSTEM_PROMPT,
            "prompt": user_msg,
            "raw_response": reply.get("text") or json.dumps(reply.get("parsed")),
            "goals": [g.model_dump() for g in obs.goals],
        })

    return obs


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
