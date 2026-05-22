"""
agent6.py — PMDA architecture (Session 6).

Usage:
    python agent6.py "<query>"

The loop:
  memory.remember(query)
  loop:
    hits = memory.read(query, history)
    obs  = perception.observe(...)
    if obs.all_done: break
    goal = obs.next_unfinished()
    attached = [artifact bytes if goal.attach_artifact_id]
    out  = decision.next_step(goal, hits, attached, history, tools)
    if out.is_answer: record in history; continue
    result_text, art_id = await action.execute(session, out.tool_call)
    memory.record_outcome(...)
    history.append(...)
  return final_answer_from(history)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure stdout/stderr handle full Unicode on Windows (cp1252 terminal)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import action
import decision
import memory
import perception
from action import artifacts
from decision import mcp_tools_for_decision
from schemas import Goal

MAX_ITERATIONS = 15
GATEWAY_URL = "http://localhost:8101"
LOGS_DIR = Path("logs")

SCENARIO_LABELS = {
    1:   "Claude Shannon Wikipedia — artifact attach",
    2:   "Tokyo activities + weather — multi-goal",
    3.1: "Mom's birthday Run 1 — durable memory write",
    3.2: "Mom's birthday Run 2 — cross-run recall",
    4:   "Python asyncio best practices — multi-artifact synthesis",
}
SCENARIO_EXPECTED_ITERS = {1: 3, 2: 6, 3.1: 4, 3.2: 2, 4: 7}


# -- Gateway health check -----------------------------------------------------

def ensure_gateway() -> None:
    try:
        r = httpx.get(f"{GATEWAY_URL}/v1/routers", timeout=5)
        r.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"LLM Gateway V3 is not reachable at {GATEWAY_URL}.\n"
            "Start it with:\n"
            "  cd llm_gatewayV3 && python -m uvicorn main:app --port 8101\n"
            f"(original error: {exc})"
        ) from exc


# -- MCP session context manager ----------------------------------------------

@asynccontextmanager
async def mcp_session():
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("mcp_server.py"))],
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def load_tools(session: ClientSession):
    return (await session.list_tools()).tools


# -- JSONL helpers ------------------------------------------------------------

def _next_scenario_number() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    existing = list(LOGS_DIR.glob("scenario_*.jsonl"))
    return len(existing) + 1


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


# -- Final answer assembly ----------------------------------------------------

def final_answer_from(history: list[dict]) -> str:
    answers = [e["text"] for e in history if e.get("kind") == "answer"]
    if not answers:
        return "No answer produced."
    if len(answers) == 1:
        return answers[0]
    return "\n\n---\n\n".join(answers)


# -- Main loop ----------------------------------------------------------------

async def run(query: str, scenario_num: float | int | None = None) -> str:
    ensure_gateway()

    run_id = uuid.uuid4().hex[:8]
    history: list[dict] = []
    prior_goals: list[Goal] = []

    # Determine scenario number and JSONL log path
    snum = scenario_num or _next_scenario_number()
    timestamp = int(time.time())
    jsonl_path = LOGS_DIR / f"scenario_{snum}_{timestamp}.jsonl"

    print(f"\n{'='*70}")
    print(f"agent6.py  run_id={run_id}  scenario={snum}")
    print(f"query: {query}")
    print(f"log:   {jsonl_path}")
    print(f"{'='*70}")

    _append_jsonl(jsonl_path, {
        "type": "run_start",
        "scenario": snum,
        "query": query,
        "run_id": run_id,
        "timestamp": timestamp,
    })

    # Classify and store the user query in durable memory
    memory.remember(query, source="user_query", run_id=run_id)

    async with mcp_session() as session:
        mcp_tools_raw = await load_tools(session)
        tools = mcp_tools_for_decision(mcp_tools_raw)
        print(f"[mcp] {len(mcp_tools_raw)} tools: {[t.name for t in mcp_tools_raw]}")

        for it in range(1, MAX_ITERATIONS + 1):
            print(f"\n--- iteration {it} -------------------------------------------")

            hits = memory.read(query, history)

            # Retry perception/decision on transient gateway errors (502/503)
            for _retry in range(3):
                try:
                    obs = perception.observe(
                        query, hits, history, prior_goals, run_id,
                        jsonl_path=jsonl_path,
                        iteration=it,
                    )
                    break
                except Exception as exc:
                    if _retry < 2:
                        print(f"[perception] transient error ({exc}), retrying in 30s...")
                        await asyncio.sleep(30)
                    else:
                        raise
            prior_goals = obs.goals

            print(f"[perception] goals ({len(obs.goals)}):")
            for g in obs.goals:
                status = "[done]" if g.done else "[todo]"
                attach = f" [attach={g.attach_artifact_id}]" if g.attach_artifact_id else ""
                print(f"  {status} {g.text}{attach}")

            if obs.all_done:
                print("[perception] all goals done - exiting loop")
                break

            goal = obs.next_unfinished()
            print(f"[decision] active goal: {goal.text}")

            attached: list[tuple[str, bytes]] = []
            if goal.attach_artifact_id and artifacts.exists(goal.attach_artifact_id):
                blob = artifacts.get_bytes(goal.attach_artifact_id)
                attached.append((goal.attach_artifact_id, blob))
                print(f"[decision] attached artifact {goal.attach_artifact_id} ({len(blob)} bytes)")

            for _retry in range(3):
                try:
                    out = decision.next_step(
                        goal, hits, attached, history, tools,
                        jsonl_path=jsonl_path,
                        iteration=it,
                    )
                    break
                except Exception as exc:
                    if _retry < 2:
                        print(f"[decision] transient error ({exc}), retrying in 30s...")
                        await asyncio.sleep(30)
                    else:
                        raise

            if out.is_answer:
                print(f"[decision] -> ANSWER: {out.answer[:200]!r}")
                history.append({
                    "iter": it,
                    "kind": "answer",
                    "goal_id": goal.id,
                    "text": out.answer,
                })
                continue

            tc = out.tool_call
            print(f"[decision] -> TOOL: {tc.name}({json.dumps(tc.arguments)[:120]})")

            result_text, art_id = await action.execute(session, tc)
            print(f"[action]   result_preview: {result_text[:200]!r}")
            if art_id:
                print(f"[action]   stored artifact: {art_id}")

            memory.record_outcome(
                tool_call=tc,
                result_text=result_text,
                artifact_id=art_id,
                run_id=run_id,
                goal_id=goal.id,
            )
            history.append({
                "iter": it,
                "kind": "action",
                "goal_id": goal.id,
                "tool": tc.name,
                "arguments": tc.arguments,
                "result_descriptor": result_text[:300],
                "artifact_id": art_id,
            })
        else:
            print(f"[agent6] reached MAX_ITERATIONS={MAX_ITERATIONS}")

    answer = final_answer_from(history)
    iters_used = max((e["iter"] for e in history), default=0)
    iters_expected = SCENARIO_EXPECTED_ITERS.get(snum, 7)
    passed = iters_used <= iters_expected * 2 and answer != "No answer produced."

    # run_result tracks the iteration-count pass/fail rule from s6.md:
    # "Queries that exceed twice the expected iteration count are not considered passing."
    # PoP (Prompt of Prompts) = the system prompts in each perception/decision record above.
    # The perception and decision JSONL records with system_prompt are the PoP deliverable.
    _append_jsonl(jsonl_path, {
        "type": "run_result",
        "scenario": snum,
        "query": query,
        "all_goals_done": obs.all_done,
        "iterations_used": iters_used,
        "iterations_expected": iters_expected,
        "pass_threshold": iters_expected * 2,
        "rule": "iters_used <= iters_expected * 2  (from s6.md assignment section)",
        "answer_preview": answer[:500],
        "passed": passed,
    })

    print(f"\n{'='*70}")
    print(f"FINAL ANSWER:\n{answer}")
    print(f"{'='*70}")
    print(f"iterations used: {iters_used}  (expected ≤{iters_expected * 2})")
    print(f"Run result     : {'PASS' if passed else 'FAIL'}")

    return answer


# -- CLI ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="PMDA Agent6")
    parser.add_argument("query", help="The query to answer")
    parser.add_argument("--scenario", type=float, default=None,
                        help="Scenario number for JSONL log naming (auto-detected if omitted)")
    args = parser.parse_args()
    asyncio.run(run(args.query, scenario_num=args.scenario))


if __name__ == "__main__":
    main()
