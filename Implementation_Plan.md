# PMDA Agent6 — Session 6 Implementation Plan

> **Status: COMPLETE.** All five scenarios pass PoP validation. Documentation delivered.
> This document reflects the *actual* implementation — deviations from the original plan are noted.

## Context

Build the PMDA (Memory, Perception, Decision, Action) architecture from scratch so that
five target scenarios (A, B, C1, C2, D) pass within 2× the expected iteration counts.
All code matches the exact signatures, types, and structural choices shown in the PDF.

**Key setup changes (from next_steps.txt):**
- `llm_gatewayV3/` has been moved to the project root (not inside `session_notes/`)
- Available API keys: Gemini, Groq, Cerebras, NVIDIA, OpenRouter, GitHub, Ollama
- `providers.py` updated for Ollama's `gemma4` model support (comment + new line pattern)
- JSONL trace logs written per run for PoP (Prompt of Prompts) validation
- Final deliverables: `Assignment_Summary.md`, `README.md`, `youtube_narration.txt` ✓

---

## File Layout (actual)

```
PMDA-Architecture/
├── .env                            ← actual keys (gitignored)
├── .env.example                    ← template with all vars + placeholders
├── .gitignore                      ← excludes state/, .env, logs/
├── pyproject.toml
├── schemas.py
├── memory.py
├── perception.py
├── decision.py
├── action.py
├── agent6.py
├── mcp_server.py                   ← httpx-based (NOT crawl4ai — see §8)
├── run.py                          ← scenario runner (A/B/C1/C2/D)
├── simulate_agent5_failure.py      ← Step 0
├── Assignment_Summary.md           ✓ delivered
├── README.md                       ✓ delivered
├── youtube_narration.txt           ✓ delivered
├── state/                          ← auto-created at runtime (gitignored)
│   ├── memory.json
│   └── artifacts/
├── logs/                           ← JSONL trace logs per run (gitignored)
│   └── scenario_<N>_<timestamp>.jsonl
├── llm_gatewayV3/                  ← moved from session_notes/ to root
│   ├── client.py
│   ├── main.py
│   ├── providers.py
│   └── ...
└── session_notes/                  ← reference only
    ├── agent5.py
    ├── next_steps.txt
    └── ...
```

**LLM import** (updated path — `llm_gatewayV3` is at root):
```python
sys.path.insert(0, str(Path(__file__).parent / "llm_gatewayV3"))
from client import LLM
```

---

## 0. Gateway Setup Changes

### `.env` and `.env.example`

Both files live at the project root. `.env.example` is committed; `.env` is gitignored.

```dotenv
GATEWAY_V3_PORT=8101

GEMINI_API_KEY=<key>
GEMINI_MODEL=gemini-2.5-flash

GROQ_API_KEY=<key>
GROQ_MODEL=openai/gpt-oss-120b

OLLAMA_MODEL=gemma4:e4b
OLLAMA_URL=http://localhost:11434

NVIDIA_API_KEY=<key>
NVIDIA_MODEL=meta/llama-3.1-70b-instruct   # ← was deepseek-ai/deepseek-v3.2 (404)

CEREBRAS_API_KEY=<key>
CEREBRAS_MODEL=zai-glm-4.7

OPEN_ROUTER_API_KEY=<key>
OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free

GITHUB_ACCESS_TOKEN=<key>
GITHUB_MODEL=openai/gpt-4.1-mini

LLM_ORDER=gemini,cerebras,groq,nvidia,openrouter,github,ollama
ROUTER_ORDER=cerebras,groq,nvidia,github
ROUTER_GROQ_MODEL=llama-3.3-70b-versatile
ROUTER_NVIDIA_MODEL=nvidia/llama-3.1-nemotron-nano-8b-v1
ROUTER_CEREBRAS_MODEL=llama3.1-8b
ROUTER_GITHUB_MODEL=microsoft/Phi-4-mini-instruct
```

### `providers.py` — update for `gemma4` (comment + new line pattern)

```python
# Old:
# OLLAMA_TOOL_MODELS = ("llama3.1", "llama3.2", ..., "firefunction")
# Updated: added gemma4 for local gemma4:e4b tool support
OLLAMA_TOOL_MODELS = ("llama3.1", "llama3.2", ..., "firefunction", "gemma4")
```

### NVIDIA model fix *(discovered during implementation)*

`deepseek-ai/deepseek-v3.2` returns HTTP 404 on NVIDIA NIM. Fixed in both
`providers.py` default and `.env`:

```python
# Old (comment preserved):
# NvidiaProvider(k, os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v3.2"))
# Updated: confirmed available model on NIM API
NvidiaProvider(k, os.getenv("NVIDIA_MODEL", "meta/llama-3.1-70b-instruct"))
```

---

## 1. pyproject.toml

```toml
[project]
name = "pmda-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "pydantic>=2.0",
    "mcp>=1.0",
    "httpx>=0.27",
    "tzdata>=2024.1",    # ← added: Windows has no system timezone database
]
```

---

## 2. schemas.py — exact PDF Pydantic contracts

```python
class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str           # one short human-readable line
    value: dict               # structured payload  ← PDF says dict, NOT str
    artifact_id: str | None
    source: str
    run_id: str
    goal_id: str | None
    confidence: float
    created_at: datetime

class Artifact(BaseModel):
    id: str                   # "art:<sha256-prefix>"
    content_type: str
    size_bytes: int
    source: str
    descriptor: str

class Goal(BaseModel):
    id: str
    text: str
    done: bool
    attach_artifact_id: str | None   # filled by loop after artifact_index mapping

class Observation(BaseModel):
    goals: list[Goal]

    @property
    def all_done(self) -> bool: ...
    def next_unfinished(self) -> Goal | None: ...

class ToolCall(BaseModel):
    name: str
    arguments: dict

class DecisionOutput(BaseModel):
    answer: str | None
    tool_call: ToolCall | None

    @property
    def is_answer(self) -> bool: ...

# Internal LLM schema for Perception
class PerceptionGoal(BaseModel):
    text: str
    done: bool
    artifact_index: int | None   # integer → loop maps to art: handle

class PerceptionObservation(BaseModel):
    goals: list[PerceptionGoal]

# Internal schema for memory.remember()
class MemoryClassification(BaseModel):
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict
    confidence: float
```

---

## 3. ArtifactStore (in action.py)

```
Handle format:  art:<sha256-16hex>
Threshold:      4096 bytes
Storage:        state/artifacts/<sha>.bin  +  state/artifacts/<sha>.json
Deduplication:  identical content → same handle, skip write
```

```python
class ArtifactStore:
    def put(self, blob: bytes, *, content_type, source, descriptor) -> str: ...
    def get_bytes(self, artifact_id: str) -> bytes: ...
    def exists(self, artifact_id: str) -> bool: ...
    def get_meta(self, artifact_id: str) -> Artifact: ...

artifacts = ArtifactStore()   # module-level singleton
```

---

## 4. memory.py

**Persistence:** `state/memory.json` — list of serialized `MemoryItem`. Loaded at import,
written back after every `remember()` / `record_outcome()` call. Survives across runs.

### `read(query, history, kinds=None, top_k=8)` — NO LLM
Tokenises query → keyword overlap score → top-k by overlap desc, recency desc.
Falls back to most-recent-top-k if no overlaps.

### `remember(raw_text, source, run_id, goal_id)` — ONE gateway call
`auto_route="memory"`, prompt-based JSON (no `response_format` — avoids Gemini 503s).
LLM returns `MemoryClassification`; Python assigns id, created_at, appends and saves.

### `record_outcome(tool_call, result_text, artifact_id, run_id, goal_id)` — NO LLM
`kind="tool_outcome"`, `keywords=[tool_name] + first-3-arg-values`,
`value={"tool":..., "args":..., "result": text[:500]}`.

### `filter(kinds, goal_id, recent)` and `relevant(query, kinds, top_k)` per PDF

---

## 5. perception.py

**LLM config:** `auto_route="perception"`, `temperature=1.0` (low temp causes Gemini loops),
no `response_format` (causes 503s when dict fields present), prompt-based JSON with fence stripping.

### `observe(query, hits, history, prior_goals, run_id)` → `Observation`

**First call** (`prior_goals == []`): decompose into 2–5 atomic goals.
**Subsequent calls**: update done flags from history, set artifact_index on first unfinished goal.

#### Key Python safety nets *(discovered during implementation)*

**Artifact hit filter** — prevent cross-run contamination:
```python
artifact_hits = [(i, h) for i, h in enumerate(hits)
                 if h.artifact_id and h.run_id == run_id]
```

**Minimum goal count** (first call only):
```python
if not prior_goals and len(goals) < 2:
    goals.append(Goal(text="Synthesize all gathered information ...", done=False))
```

**Sticky-done enforcement** — goals never revert from done=True:
```python
for i, g in enumerate(goals):
    if i < len(prior_goals) and prior_goals[i].done:
        g.done = True
```

**Force-done safety net** — ≥2 kind=answer entries for a goal_id → mark done:
```python
answer_counts = {}
for e in history:
    if e.get("kind") == "answer":
        answer_counts[e.get("goal_id", "")] = answer_counts.get(..., 0) + 1
for g in goals:
    if not g.done and answer_counts.get(g.id, 0) >= 2:
        g.done = True
```

**Two-case force-attach** (when LLM didn't set artifact_index):
```python
words = set(unfinished.text.lower().split())
# Case 1: "Fetch/Read Nth result/URL/page" → attach oldest web_search artifact
if {"fetch","read","get"} & words and {"result","url","page","link"} & words:
    ws_hits = [h for h in artifact_hits if h.value.get("tool") == "web_search"]
    if ws_hits:
        unfinished.attach_artifact_id = ws_hits[-1][1].artifact_id
# Case 2: synthesis goals → attach most recent artifact
elif words & {"synthesise","synthesize","extract","list","compare","decide","summarise"}:
    unfinished.attach_artifact_id = artifact_hits[-1][1].artifact_id
```

### Perception system prompt (key rules)
- First call: decompose into 2–5 atomic goals; never a single compound goal; one goal per fetch URL
- Subsequent: mark done from history; never flip done→undone; never reorder
- `artifact_index` is integer (0-based into INDEXED ARTIFACTS list) or null

### JSONL logging
Every `observe()` call appends:
```json
{"type": "perception", "iteration": N, "prompt": "...", "raw_response": "...", "goals": [...]}
```

---

## 6. decision.py

**Native tool-use** (NOT structured output):
```python
reply = llm.chat(
    messages=..., system=DECISION_SYSTEM,
    tools=mcp_tools, tool_choice="auto",
    auto_route="decision", temperature=1.0, max_tokens=2048,
)
tool_calls = reply.get("tool_calls") or []
if tool_calls:
    return DecisionOutput(tool_call=ToolCall(...))
return DecisionOutput(answer=reply.get("text", "").strip())
```

### Decision system prompt (6 rules — expanded from original 3)

| Rule | Summary |
|------|---------|
| 1 | Exactly one output: tool call OR answer, never both |
| 2 | `art:` handles are not file paths — never pass to read_file/fetch_url |
| 3 | Answers must be substantive (≥3 sentences or list); no meta-answers |
| 4 | Be decisive — synthesise from existing history instead of re-searching |
| 5 | When attached artifact is a web_search JSON array, parse URLs and call fetch_url on first unfetched URL |
| 6 | Hard search cap: ≥2 web_search calls for same goal → give FINAL ANSWER immediately |

### User message structure
- `CURRENT GOAL: {goal.text}`
- `MEMORY CONTEXT:` — descriptor + value preview per hit
- `HISTORY (recent 5):` — truncated to last 5 entries (was 8; reduced to stay under token limit)
- `ATTACHED ARTIFACTS:` — artifact bytes decoded UTF-8, truncated to 2500 chars (was 6000)

### JSONL logging
Every `next_step()` call appends:
```json
{"type": "decision", "iteration": N, "goal_text": "...", "prompt": "...", "raw_response": {...}, "output": {...}}
```

---

## 7. action.py — pure dispatch

```python
async def execute(session, tool_call) -> tuple[str, str | None]:
```

Three branches:
1. Any arg value starts with `art:` → return error string (do not raise; error enters history)
2. `session.call_tool()` → join `.text` from all content items
3. `len(result.encode()) > 4096` → store as artifact; return (preview, art_id)

`memory.record_outcome()` is called by agent6.py, not here — keeps Action as pure dispatcher.

---

## 8. mcp_server.py — httpx-based fetch_url

**Key deviation from original plan:** `fetch_url` was rewritten from crawl4ai to httpx.

**Root cause:** crawl4ai uses `os.dup2()` for stdio fd redirection in a subprocess, which
hangs indefinitely on Windows inside an MCP stdio server.

**Fix:**
- Wikipedia URLs → MediaWiki API (plain text extract, no HTML parsing)
- General URLs → httpx with browser User-Agent + BeautifulSoup HTML tag stripping

---

## 9. agent6.py — orchestrator loop

```python
MAX_ITERATIONS = 15
SCENARIO_EXPECTED_ITERS = {1: 3, 2: 6, 3: 4, 4: 2, 5: 7}

async def run(query, scenario_n=None):
    ensure_gateway()
    run_id = uuid.uuid4().hex[:8]
    memory.remember(query, source="user_query", run_id=run_id)

    async with mcp_session() as session:
        mcp_tools = await session.list_tools()
        tools = mcp_tools_for_decision(mcp_tools.tools)

        for it in range(1, MAX_ITERATIONS + 1):
            hits  = memory.read(query, history)
            obs   = perception.observe(query, hits, history, prior_goals, run_id,
                                       jsonl_path=log_path, iteration=it)
            prior_goals = obs.goals
            if obs.all_done: break

            goal     = obs.next_unfinished()
            attached = [(id, artifacts.get_bytes(id))] if goal.attach_artifact_id else []
            out      = decision.next_step(goal, hits, attached, history, tools,
                                          jsonl_path=log_path, iteration=it)

            if out.is_answer:
                history.append({"kind": "answer", "goal_id": goal.id, "text": out.answer, ...})
                continue

            result_text, art_id = await action.execute(session, out.tool_call)
            memory.record_outcome(out.tool_call, result_text, art_id, run_id, goal.id)
            history.append({"kind": "action", ...})

    # Write PoP validation record
    _append_jsonl(log_path, {"type": "pop_validation", ...})
    return final_answer_from(history)
```

**`ensure_gateway()`:** `GET http://localhost:8101/v1/routers` with 5s timeout.
**`final_answer_from(history)`:** collect all `kind=answer` entries, join with `---`.

---

## 10. run.py — scenario runner

```powershell
uv run python run.py A    # Claude Shannon Wikipedia
uv run python run.py B    # Tokyo activities + weather
uv run python run.py C1   # Mom's birthday — store
uv run python run.py C2   # Mom's birthday — recall (NO state clean between C1 and C2)
uv run python run.py D    # asyncio best practices
uv run python run.py 0    # simulate_agent5_failure.py
```

---

## 11. JSONL PoP Trace Format

Each run writes `logs/scenario_<N>_<timestamp>.jsonl`. Line types:

| Type | Key fields |
|------|-----------|
| `perception` | `iteration`, `prompt`, `raw_response`, `goals[]` |
| `decision` | `iteration`, `goal_text`, `prompt`, `raw_response`, `output` |
| `pop_validation` | `scenario`, `query`, `all_goals_done`, `iterations_used`, `iterations_expected`, `answer`, `passed` |

---

## 12. Bugs Fixed During Implementation

| # | Bug | Root cause | Fix |
|---|-----|-----------|-----|
| 1 | `fetch_url` hangs forever | crawl4ai `os.dup2()` in MCP stdio subprocess on Windows | Replaced with httpx + MediaWiki API for Wikipedia |
| 2 | Gateway 503 on `remember()` | `response_format` with dict fields → Gemini 503 | Removed `response_format`; use prompt-based JSON + fence stripping |
| 3 | Gateway 503 when Gemini on cooldown | `provider="g"` with `explicit_override=True` blocks fallback | Removed provider pin from memory and perception |
| 4 | `UnicodeEncodeError` | Windows stdout cp1252 can't encode Unicode chars (→, →) | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` |
| 5 | `ZoneInfo("Asia/Tokyo")` fails | Windows has no system timezone database | Added `tzdata>=2024.1` to dependencies |
| 6 | Groq 6000 TPM exceeded | 6000-char artifact + history + 9 tools ≈ 8000 tokens per call | Truncated artifact to 2500 chars; history to last 5 entries |
| 7 | NVIDIA HTTP 404 | `deepseek-ai/deepseek-v3.2` doesn't exist on NIM | Changed default to `meta/llama-3.1-70b-instruct` |
| 8 | Perception creates 1 compound goal | LLM ignores "never put whole query as one goal" | Python safety net: if first call returns < 2 goals, append synthesis goal |
| 9 | Goal never marked done despite answers | Perception LLM can't verify from 150-char preview | Force-done: ≥2 answers for same goal_id → mark done; increased preview to 250 chars |
| 10 | Cross-run artifact contamination | Prior run's artifacts appeared in current run's hits | Filter artifact_hits to `h.run_id == run_id` |
| 11 | "Fetch Nth result" goals have no artifact | Force-attach only triggered on SYNTHESIS_KEYWORDS | Added Case 1: attach oldest `web_search` artifact to fetch/read/get + result/url goals |

---

## 13. Validation Results — All Scenarios Pass

| Scenario | Query | Iters used | Expected ≤ | Result |
|----------|-------|-----------|------------|--------|
| A (1) | Claude Shannon Wikipedia → dates + contributions | 4 | 6 | **PASS** |
| B (2) | Tokyo activities + Saturday weather + recommendation | 6 | 12 | **PASS** |
| C1 (3) | Store mom's birthday 15 May 2026 + calendar reminders | 4 | 8 | **PASS** |
| C2 (4) | "When is mom's birthday?" — from persistent memory | 4 | 4 | **PASS** |
| D (5) | Search asyncio, fetch top 3 URLs, list consensus | 10 | 14 | **PASS** |

**PoP pass criterion:** `iters_used ≤ iters_expected × 2` AND `answer ≠ "No answer produced."`

---

## 14. Critical Pitfalls

| # | Pitfall | Fix |
|---|---------|-----|
| 1 | `MemoryItem.value` is `dict` not `str` | Classifier LLM must return structured dict via `MemoryClassification` |
| 2 | Artifact handles are `art:<sha256>` not integers | `ArtifactStore.put()` computes SHA256; handles stored as `art:<16hex>` |
| 3 | Perception LLM emits `artifact_index: int` | Loop maps integer index → actual `art:` handle → `Goal.attach_artifact_id` |
| 4 | Decision uses native tool-use, NOT response_format | `tools=mcp_tools, tool_choice="auto"` — parse `tool_calls[]` from reply |
| 5 | `temperature=0.0` causes Gemini perception loops | Always `temperature=1.0` for Perception and Decision |
| 6 | Action guard: `art:` args return error STRING, not raise | Error enters history; Perception can recover on next iteration |
| 7 | `memory.record_outcome` takes `ToolCall` object | Match PDF signature: `tool_call=out.tool_call` (not tool_name string) |
| 8 | `gemma4` not in `OLLAMA_TOOL_MODELS` | Add `"gemma4"` to the tuple using comment + new line pattern |
| 9 | `response_format` with dict fields → Gemini 503 | Use prompt-based JSON extraction; strip markdown fences manually |
| 10 | Cross-run artifact contamination in Perception | Filter `artifact_hits` to `h.run_id == run_id` before building index |
| 11 | Scenario D "Fetch Nth" goals never get URL list | Force-attach: detect fetch+url keywords → attach oldest `web_search` artifact |

---

## 15. Deliverables Checklist

| Item | Status |
|------|--------|
| All 5 scenarios pass PoP validation | ✓ |
| `schemas.py` — exact PDF Pydantic types | ✓ |
| `memory.py` — 4 functions, JSON persistence | ✓ |
| `perception.py` — decompose + track + safety nets | ✓ |
| `decision.py` — native tool-use, 6 rules | ✓ |
| `action.py` — pure dispatcher + ArtifactStore | ✓ |
| `agent6.py` — PDF loop + JSONL PoP traces | ✓ |
| `mcp_server.py` — 9-tool server (httpx) | ✓ |
| `simulate_agent5_failure.py` — Step 0 demos | ✓ |
| `run.py` — scenario runner A/B/C1/C2/D | ✓ |
| `Assignment_Summary.md` | ✓ |
| `README.md` | ✓ |
| `youtube_narration.txt` (3 min) | ✓ |
| JSONL per-run traces (`logs/scenario_N_*.jsonl`) | ✓ |
| GitHub repo name suggestion | ✓ (`pmda-agent` / `eag-v3-pmda-architecture`) |
