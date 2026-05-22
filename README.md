# PMDA Agent — Memory · Perception · Decision · Action

A structured AI agent architecture that decomposes user queries into ordered goals,
executes them step-by-step using real tools, stores results in a content-addressable
artifact store, and remembers facts across sessions.

---

## Architecture

```
  User Query
      │
      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       agent6.py  (orchestrator loop)                     │
│                                                                           │
│  for iteration in 1..15:                                                  │
│                                                                           │
│  ┌──────────────┐   goals    ┌──────────────┐  tool/answer ┌──────────┐ │
│  │  PERCEPTION  │◄──────────│    MEMORY    │              │ DECISION │ │
│  │              │            │              │◄─────────────│          │ │
│  │ observe()    │──goals────►│  read()      │   hits       │next_step │ │
│  │ decompose /  │            │  remember()  │              │          │ │
│  │ track goals  │            │  record_     │              │ tool_call│ │
│  └──────────────┘            │  outcome()   │              │ OR answer│ │
│         │                    └──────────────┘              └────┬─────┘ │
│         │ next unfinished goal + attached artifact               │        │
│         └────────────────────────────────────────►  ┌──────────▼─────┐ │
│                                                      │    ACTION      │ │
│                                                      │                │ │
│                                                      │ execute()      │ │
│                                                      │ MCP tool call  │ │
│                                                      │ >4KB → artifact│ │
│                                                      └────────────────┘ │
│                                                                           │
│  state/memory.json      ── persists across runs                          │
│  state/artifacts/*.bin  ── content-addressable blobs (art:<sha256>)      │
└─────────────────────────────────────────────────────────────────────────┘
      │
      ▼
  Final Answer (joined from all kind=answer history entries)
```

### Gateway Routing

| Layer      | auto_route    | Provider chain                              |
|------------|---------------|---------------------------------------------|
| Memory     | `memory`      | cerebras → groq → nvidia → github          |
| Perception | `perception`  | gemini → cerebras → groq → nvidia → …      |
| Decision   | `decision`    | gemini → cerebras → groq → nvidia → …      |
| Action     | *(no LLM)*    | MCP stdio server                            |

---

## Quick Start

### Prerequisites

```powershell
# 1. Install dependencies
uv sync

# 2. Copy and fill environment variables
copy .env.example .env
# Set: GEMINI_API_KEY, GROQ_API_KEY, TAVILY_API_KEY (minimum)

# 3. Start the LLM Gateway
cd llm_gatewayV3
uv run uvicorn main:app --port 8101
cd ..
```

### Run a Scenario

```powershell
uv run python run.py A    # Claude Shannon Wikipedia  (expects ≤6 iters)
uv run python run.py B    # Tokyo activities + weather (expects ≤12 iters)
uv run python run.py C1   # Mom's birthday — store    (expects ≤8 iters)
uv run python run.py C2   # Mom's birthday — recall   (expects ≤4 iters, NO clean state)
uv run python run.py D    # asyncio best practices     (expects ≤14 iters)
```

Or pass any query directly:

```powershell
uv run python agent6.py "What is the capital of France and what's the weather like there today?"
```

---

## Validation Results

| Scenario | Query | Iterations | Limit | Result |
|----------|-------|-----------|-------|--------|
| A | Fetch Wikipedia / extract Shannon's dates + contributions | 4 | ≤6 | **PASS** |
| B | Find 3 family-friendly Tokyo activities + weather recommendation | 6 | ≤12 | **PASS** |
| C1 | Remember mom's birthday (15 May 2026) + calendar reminders | 4 | ≤8 | **PASS** |
| C2 | "When is mom's birthday?" — answered from persistent memory | 4 | ≤4 | **PASS** |
| D | Search asyncio best practices, read top 3 pages, list consensus | 10 | ≤14 | **PASS** |

---

## File Structure

```
PMDA-Architecture/
├── agent6.py               # Main orchestrator loop
├── schemas.py              # Pydantic models (Goal, MemoryItem, Artifact, …)
├── memory.py               # Persistent memory: read, remember, record_outcome
├── perception.py           # Goal decomposer & tracker (LLM, temp=1.0)
├── decision.py             # Action selector (native tool-use)
├── action.py               # MCP dispatcher + ArtifactStore
├── mcp_server.py           # 9-tool MCP server (search, fetch, files, time, …)
├── run.py                  # Scenario runner (A / B / C1 / C2 / D)
├── simulate_agent5_failure.py  # Step 0: demonstrates 3 failure modes
├── pyproject.toml
├── .env.example
├── llm_gatewayV3/          # Multi-provider LLM gateway (port 8101)
│   ├── main.py
│   ├── providers.py        # Gemini, Groq, Cerebras, NVIDIA, OpenRouter, …
│   └── …
├── state/                  # Runtime state (auto-created)
│   ├── memory.json
│   └── artifacts/
│       ├── <sha256>.bin
│       └── <sha256>.json
└── logs/                   # Per-run JSONL traces (perception + decision)
    └── scenario_<N>_<ts>.jsonl
```

---

## Key Design Decisions

### Artifact Store — content-addressable, threshold-gated
Results larger than **4096 bytes** are stored as `art:<sha256-16hex>` blobs rather than
inlined in messages. Identical content deduplicates automatically. This prevents context
explosion on large web pages (Wikipedia articles are ~34 KB).

### Goal IDs — sticky and positional
Perception re-uses prior goal IDs positionally. A Python safety net enforces that goals
never flip from `done=True` back to `done=False` (sticky-done). A second safety net
force-marks a goal done after ≥2 decision answers for the same goal ID.

### Artifact attachment — two force-attach cases
1. **"Fetch Nth result" goals**: the web_search results artifact is auto-attached so
   Decision can parse the JSON URL list and call `fetch_url` on the correct entry.
2. **Synthesis goals** (extract, synthesise, list, compare): the most recent artifact
   is attached so Decision has the raw content to work from.

### Decision uses native tool-use, not structured output
The gateway is called with `tools=mcp_tools, tool_choice="auto"`. When the response
contains `tool_calls[]`, Action executes the first call. When it contains plain text,
the text becomes the goal's answer. This matches how production agents work.

### Memory survives across runs
`state/memory.json` is loaded at startup and appended on every `remember()` /
`record_outcome()` call. Scenario C2 answers "When is mom's birthday?" from memory
created in a previous process — zero tool calls required.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Google Gemini (primary worker) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Worker model |
| `GROQ_API_KEY` | — | Groq fallback worker |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Worker model |
| `CEREBRAS_API_KEY` | — | Cerebras (60K TPM free tier) |
| `NVIDIA_API_KEY` | — | NVIDIA NIM |
| `NVIDIA_MODEL` | `meta/llama-3.1-70b-instruct` | Worker model |
| `TAVILY_API_KEY` | — | Web search (primary) |
| `LLM_ORDER` | `gemini,cerebras,groq,nvidia,openrouter,github,ollama` | Worker failover chain |
| `ROUTER_ORDER` | `cerebras,groq,nvidia,github` | Router pool |

---

## Suggested GitHub Repository Name

**`pmda-agent`** or **`eag-v3-pmda-architecture`**
