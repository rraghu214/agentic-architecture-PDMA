#!/usr/bin/env python3
"""
generate_flow.py — Rich HTML sequence diagram from a PMDA JSONL trace log.

Usage:
    python generate_flow.py logs/scenario_5_1779024341.jsonl
    # Writes: console_output/scenario_5_1779024341_flow.html
    # Or open directly in a browser.
"""
import json
import re
import sys
from html import escape
from pathlib import Path

# ── Actor definitions ─────────────────────────────────────────────────────────
ACTORS  = ["agent6 loop", "Memory", "Perception", "Artifacts", "Decision", "Action (MCP)"]
COLORS  = ["#4a5568",     "#6b46c1", "#2b6cb0",    "#c05621",   "#276749",  "#c53030"]
BG      = ["#f7fafc",     "#faf5ff", "#ebf8ff",    "#fffaf0",   "#f0fff4",  "#fff5f5"]
IDX      = {a: i for i, a in enumerate(ACTORS)}
N_ACTORS = len(ACTORS)

LOOP, MEM, PERC, ART, DEC, ACT = 0, 1, 2, 3, 4, 5

# ── Helpers ───────────────────────────────────────────────────────────────────

def e(s: str) -> str:
    return escape(str(s), quote=True)

def load_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records

def read_source_prompts(base: Path) -> tuple[str, str]:
    """Read SYSTEM_PROMPT and DECISION_SYSTEM from source files as fallback."""
    def extract(src: str, var: str) -> str:
        m = re.search(rf'{var}\s*=\s*"""\\\n(.*?)"""', src, re.DOTALL)
        return m.group(1).strip() if m else ""

    perc_src = (base / "perception.py").read_text(encoding="utf-8") if (base / "perception.py").exists() else ""
    dec_src  = (base / "decision.py").read_text(encoding="utf-8")   if (base / "decision.py").exists()  else ""
    return extract(perc_src, "SYSTEM_PROMPT"), extract(dec_src, "DECISION_SYSTEM")

def parse_sections(prompt: str) -> dict[str, str]:
    """Split a prompt into named sections (USER QUERY:, PRIOR GOALS:, etc.)."""
    section_re = re.compile(
        r"^(USER QUERY|PRIOR GOALS|RUN HISTORY|RELEVANT MEMORY|INDEXED ARTIFACTS"
        r"|CURRENT GOAL|MEMORY CONTEXT|HISTORY|ATTACHED ARTIFACTS)\s*(?:\([^)]*\))?\s*[:\-—]+\s*",
        re.MULTILINE,
    )
    parts, last_key, last_pos = {}, None, 0
    for m in section_re.finditer(prompt):
        if last_key:
            parts[last_key] = prompt[last_pos:m.start()].strip()
        last_key = m.group(1)
        last_pos = m.end()
    if last_key:
        parts[last_key] = prompt[last_pos:].strip()
    return parts

def extract_history_actions(decision_prompt: str) -> list[dict]:
    """Parse the HISTORY section of a decision prompt into action entries."""
    sections = parse_sections(decision_prompt)
    hist_text = sections.get("HISTORY") or sections.get("RUN HISTORY", "")
    actions = []
    for line in hist_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"iter=(\d+)\s+kind=(\w+)\s+(?:tool=(\w+)\s+result_preview=(.+)|text_preview=(.+))", line)
        if m:
            actions.append({
                "iter":   int(m.group(1)),
                "kind":   m.group(2),
                "tool":   m.group(3) or "",
                "result": (m.group(4) or m.group(5) or "").strip(),
            })
    return actions

# ── HTML building blocks ──────────────────────────────────────────────────────

def collapsible(summary: str, body: str, color: str = "#4a5568", open_: bool = False) -> str:
    op = " open" if open_ else ""
    return (
        f'<details{op} style="margin:4px 0;">'
        f'<summary style="cursor:pointer;font-weight:600;color:{color};padding:4px 8px;'
        f'background:rgba(0,0,0,0.04);border-radius:4px;">{summary}</summary>'
        f'<div class="detail-body">{body}</div>'
        f'</details>'
    )

def code_block(text: str) -> str:
    return f'<pre class="code">{e(text)}</pre>'

def badge(text: str, color: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            f'font-size:11px;font-weight:700;background:{color};color:#fff;">{e(text)}</span>')

def goal_list(goals: list[dict]) -> str:
    rows = []
    for g in goals:
        done   = g.get("done", False)
        tick   = "✓" if done else "○"
        color  = "#276749" if done else "#2b6cb0"
        attach = f' <small style="color:#c05621">⊕ {g["attach_artifact_id"]}</small>' if g.get("attach_artifact_id") else ""
        rows.append(
            f'<div style="padding:3px 8px;border-left:3px solid {color};margin:2px 0;">'
            f'<span style="color:{color};font-weight:700">{tick}</span> '
            f'<code style="font-size:12px">{e(g["id"][:8])}</code> '
            f'{e(g["text"])}{attach}'
            f'</div>'
        )
    return "".join(rows)

def arrow_row(from_idx: int, to_idx: int, label: str, dashed: bool = False,
              extra: str = "") -> str:
    """Render a single message-arrow row spanning from_idx → to_idx."""
    left  = min(from_idx, to_idx)
    right = max(from_idx, to_idx)
    going_right = to_idx > from_idx

    # 6-column grid; span from left+1 to right+2 (CSS grid is 1-indexed)
    grid_col_start = left + 1
    grid_col_end   = right + 2

    border_style = "dashed" if dashed else "solid"
    arrow_char   = "►" if going_right else "◄"
    label_align  = "left" if going_right else "right"
    from_color   = COLORS[from_idx]
    to_color     = COLORS[to_idx]
    bg_color      = BG[to_idx if not dashed else from_idx]

    # Build left spacer columns
    spacers = "".join(
        f'<div class="lifeline" style="background:{BG[i]};"></div>'
        for i in range(N_ACTORS) if i < left or i > right
    )

    arrow_html = (
        f'<div class="arrow-cell" style="grid-column:{grid_col_start}/{grid_col_end};'
        f'background:{bg_color};border-radius:6px;padding:6px 10px;">'
        f'<div style="border-bottom:2px {border_style} {from_color};position:relative;padding-bottom:4px;">'
        f'<span style="position:absolute;{"left" if going_right else "right"}:0;'
        f'color:{to_color};font-weight:900;font-size:16px;">{arrow_char}</span>'
        f'<span style="color:{from_color};font-size:12px;font-weight:600;'
        f'padding:0 20px;">{e(label)}</span>'
        f'</div>'
        f'{extra}'
        f'</div>'
    )

    # We'll use a 6-col CSS grid row for each arrow
    # Simpler: just use a flat div with margin-based indentation + border
    col_pct = 100 / N_ACTORS
    margin_l = f"{left * col_pct:.1f}%"
    width    = f"{(right - left + 1) * col_pct:.1f}%"

    align_side = "text-align:left;" if going_right else "text-align:right;"
    return (
        f'<div class="arrow-row">'
        f'<div class="arrow-span" style="margin-left:{margin_l};width:{width};'
        f'background:{bg_color};border-radius:6px;padding:6px 10px;'
        f'border-bottom:2px {border_style} {from_color};">'
        f'<span class="arrow-head" style="color:{to_color};">'
        f'{"" if going_right else arrow_char}</span>'
        f'<span class="arrow-label" style="color:{from_color};{align_side}">'
        f'{e(label)}</span>'
        f'<span class="arrow-head" style="color:{to_color};">'
        f'{"►" if going_right else ""}</span>'
        f'{extra}'
        f'</div>'
        f'</div>'
    )

def step_card(title: str, actor_idx: int, rows: list[str]) -> str:
    color = COLORS[actor_idx]
    bg    = BG[actor_idx]
    return (
        f'<div class="step-card" style="border-left:4px solid {color};background:{bg};">'
        f'<div class="step-title" style="color:{color};">{title}</div>'
        + "".join(rows) +
        f'</div>'
    )

def lifeline_row(height: str = "8px") -> str:
    cells = "".join(
        f'<div style="height:{height};background:{BG[i]};'
        f'border-left:2px dashed {COLORS[i]}44;"></div>'
        for i in range(N_ACTORS)
    )
    return f'<div class="lifeline-row">{cells}</div>'

# ── Main diagram builder ──────────────────────────────────────────────────────

def build_iteration(
    it: int,
    perc: dict,
    dec: dict,
    perc_sys: str,
    dec_sys: str,
    prev_dec: dict | None,
) -> str:
    html_parts = []
    html_parts.append(
        f'<div class="iter-header">── Iteration {it} ──</div>'
    )

    # ── MEMORY READ ────────────────────────────────────────────────────────────
    perc_sections = parse_sections(perc["prompt"])
    mem_content   = perc_sections.get("RELEVANT MEMORY", "")
    art_index     = perc_sections.get("INDEXED ARTIFACTS", "")
    history_text  = perc_sections.get("RUN HISTORY", "")
    query_text    = perc_sections.get("USER QUERY", "")

    mem_body = (
        f'<div style="margin:4px 0;">'
        f'<strong style="color:#6b46c1;">Query:</strong> {e(query_text)}</div>'
        + (f'<div style="margin-top:6px"><strong style="color:#6b46c1;">Memory hits returned:</strong>'
           f'{code_block(mem_content)}</div>' if mem_content else
           '<div style="color:#999;font-size:12px;">No memory hits</div>')
    )
    html_parts.append(step_card(
        f'① Memory Read — agent6 loop → Memory → agent6 loop',
        MEM,
        [
            f'<div class="msg-arrow">'
            f'<span class="from-actor">agent6 loop</span>'
            f' <span class="arr">────────────────►</span> '
            f'<span class="to-actor" style="color:{COLORS[MEM]}">Memory</span>'
            f' : <code>read(query, history)</code></div>',
            collapsible("↩ hits[] returned (handles + descriptors)", mem_body, COLORS[MEM]),
        ]
    ))

    # ── PERCEPTION ─────────────────────────────────────────────────────────────
    prior_goals_text = perc_sections.get("PRIOR GOALS", "")
    goals_list   = goal_list(perc.get("goals", []))
    raw_resp     = perc.get("raw_response", "")
    sys_p        = perc.get("system_prompt", perc_sys)

    perc_call = (
        f'observe(query, hits, history, prior_goals={len(perc.get("goals",[])) > 0}, run_id)'
    )

    first_call = not prior_goals_text.strip()
    observe_note = (
        badge("FIRST CALL — decompose query into goals", COLORS[PERC])
        if first_call else
        badge("SUBSEQUENT CALL — update done flags from history", "#6b46c1")
    )

    perc_body = (
        f'<div style="margin:6px 0;">{observe_note}</div>'
        + collapsible(
            "📋 PoP — Perception System Prompt (Prompt of Prompts)",
            code_block(sys_p or "(not captured in this log — start gateway and re-run to embed)"),
            COLORS[PERC],
        )
        + collapsible(
            "📨 User Message sent to LLM",
            code_block(perc["prompt"]),
            COLORS[PERC],
        )
        + (collapsible(
            "📌 Prior goals (input)",
            code_block(prior_goals_text),
            "#6b46c1",
        ) if prior_goals_text else "")
        + (collapsible(
            "📜 Run history passed in",
            code_block(history_text),
            "#6b46c1",
        ) if history_text else "")
        + (collapsible(
            "🗄 Indexed artifacts available",
            code_block(art_index),
            COLORS[ART],
        ) if art_index else "")
        + collapsible(
            "🤖 LLM raw response → goal JSON",
            code_block(raw_resp if isinstance(raw_resp, str) else json.dumps(raw_resp, indent=2)),
            "#2b6cb0",
        )
    )

    html_parts.append(step_card(
        f'② Perception — agent6 loop → Perception → agent6 loop',
        PERC,
        [
            f'<div class="msg-arrow">'
            f'<span class="from-actor">agent6 loop</span>'
            f' <span class="arr">───────────────────────────────────────────►</span> '
            f'<span class="to-actor" style="color:{COLORS[PERC]}">Perception</span>'
            f' : <code>{e(perc_call)}</code></div>',
            perc_body,
            f'<div style="margin-top:8px;"><strong style="color:{COLORS[PERC]}">Observation returned — Goals:</strong></div>',
            f'<div style="margin:4px 0;">{goals_list}</div>',
        ]
    ))

    # ── ARTIFACT FETCH (conditional) ───────────────────────────────────────────
    dec_sections = parse_sections(dec["prompt"])
    attached_text = dec_sections.get("ATTACHED ARTIFACTS", "")

    # Find which goal has an artifact attached
    goal_with_attach = next(
        (g for g in perc.get("goals", []) if g.get("attach_artifact_id") and not g.get("done")),
        None,
    )

    if goal_with_attach or attached_text:
        art_id = (goal_with_attach or {}).get("attach_artifact_id", "")
        art_body = (
            f'<div>Artifact handle: <code style="color:{COLORS[ART]}">{e(art_id)}</code></div>'
            + (collapsible(
                "📦 Artifact content (truncated to 2500 chars) passed to Decision",
                code_block(attached_text[:3000] + ("..." if len(attached_text) > 3000 else "")),
                COLORS[ART],
            ) if attached_text else "")
        )
        html_parts.append(step_card(
            f'③ Artifact Fetch — agent6 loop ↔ Artifacts [goal has attachment]',
            ART,
            [
                f'<div class="msg-arrow">'
                f'<span class="from-actor">agent6 loop</span>'
                f' <span class="arr">──────────────────────────────────────────────────────────────►</span> '
                f'<span class="to-actor" style="color:{COLORS[ART]}">Artifacts</span>'
                f' : <code>get_bytes({e(art_id[:30])})</code></div>',
                art_body,
            ]
        ))

    # ── DECISION ───────────────────────────────────────────────────────────────
    sys_d      = dec.get("system_prompt", dec_sys)
    goal_text  = dec.get("goal", "")
    dec_prompt = dec.get("prompt", "")
    raw_dec    = dec.get("raw_response", {})
    output     = dec.get("output", {})
    provider   = raw_dec.get("provider", "?")
    model      = raw_dec.get("model", "?")
    hist_dec   = dec_sections.get("HISTORY", "")

    tool_calls = raw_dec.get("tool_calls") or []
    answer_text = (output.get("answer") or raw_dec.get("text") or "").strip()
    is_answer = not tool_calls and bool(answer_text)

    if tool_calls:
        tc = tool_calls[0]
        result_badge = (
            f'<div style="margin:6px 0;">'
            + badge("TOOL CALL", COLORS[ACT])
            + f' <code style="font-size:13px;color:{COLORS[ACT]};">'
            f'{e(tc["name"])}({e(json.dumps(tc.get("arguments",{})))})</code></div>'
        )
    else:
        result_badge = (
            f'<div style="margin:6px 0;">'
            + badge("FINAL ANSWER", COLORS[DEC])
            + f'</div>'
            + f'<div class="answer-box">{e(answer_text[:800])}</div>'
        )

    dec_body = (
        f'<div style="margin:4px 0;">Active goal: <strong>{e(goal_text)}</strong></div>'
        + f'<div style="font-size:12px;color:#666;margin:2px 0;">Provider: <code>{e(provider)}</code>  Model: <code>{e(model)}</code></div>'
        + collapsible(
            "📋 PoP — Decision System Prompt (Prompt of Prompts)",
            code_block(sys_d or "(not captured — re-run with updated code to embed)"),
            COLORS[DEC],
        )
        + collapsible(
            "📨 User Message sent to LLM",
            code_block(dec_prompt),
            COLORS[DEC],
        )
        + (collapsible("📜 History seen by Decision", code_block(hist_dec), "#6b46c1") if hist_dec else "")
        + collapsible(
            "🤖 LLM raw response",
            code_block(json.dumps(raw_dec, indent=2)),
            "#2b6cb0",
        )
        + result_badge
    )

    html_parts.append(step_card(
        f'{"④" if (goal_with_attach or attached_text) else "③"} Decision — agent6 loop → Decision → agent6 loop',
        DEC,
        [
            f'<div class="msg-arrow">'
            f'<span class="from-actor">agent6 loop</span>'
            f' <span class="arr">────────────────────────────────────────────────────────────────────────────────────────►</span> '
            f'<span class="to-actor" style="color:{COLORS[DEC]}">Decision</span>'
            f' : <code>next_step(goal, hits, attached, history, tools)</code></div>',
            dec_body,
        ]
    ))

    # ── ACTION (if tool call) ──────────────────────────────────────────────────
    if tool_calls:
        tc   = tool_calls[0]
        args = tc.get("arguments", {})

        # Try to find the result in the next iteration's perception/decision history
        result_preview = "(result visible in next iteration's HISTORY)"

        step_num = "⑤" if (goal_with_attach or attached_text) else "④"
        act_body = (
            f'<div style="margin:4px 0;">Tool: <code style="color:{COLORS[ACT]};font-weight:700;">'
            f'{e(tc["name"])}</code></div>'
            + f'<div>Arguments: <code>{e(json.dumps(args))}</code></div>'
            + f'<div style="margin-top:4px;color:#666;font-size:12px;">'
            f'→ Memory: <code>record_outcome(tool_call, result, artifact_id)</code></div>'
            + f'<div style="color:#999;font-size:12px;margin-top:4px;">{result_preview}</div>'
        )

        html_parts.append(step_card(
            f'{step_num} Action (MCP) — agent6 loop → Action (MCP) + Memory record',
            ACT,
            [
                f'<div class="msg-arrow">'
                f'<span class="from-actor">agent6 loop</span>'
                f' <span class="arr">──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────►</span> '
                f'<span class="to-actor" style="color:{COLORS[ACT]}">Action (MCP)</span>'
                f' : <code>execute({e(tc["name"])}({e(json.dumps(args))[:80]}))</code></div>',
                act_body,
            ]
        ))

    return "\n".join(html_parts)


def build_html(records: list[dict], jsonl_name: str, perc_sys: str, dec_sys: str) -> str:
    run_start  = next((r for r in records if r["type"] == "run_start"), {})
    pop        = next((r for r in records if r["type"] in ("run_result", "pop_validation")), {})
    scenario   = run_start.get("scenario", "?")
    query      = run_start.get("query", "")
    run_id     = run_start.get("run_id", "")
    ts         = run_start.get("timestamp", "")

    iters_used = pop.get("iterations_used", "?")
    iters_exp  = pop.get("iterations_expected", "?")
    passed     = pop.get("passed", False)
    answer_prev= pop.get("answer_preview", "")
    all_done   = pop.get("all_goals_done", False)

    pop_color  = "#276749" if passed else "#c53030"
    pop_badge  = badge("PASS ✓" if passed else "FAIL ✗", pop_color)

    # Group by iteration
    perc_by_iter: dict[int, dict] = {}
    dec_by_iter:  dict[int, dict] = {}
    for r in records:
        it = r.get("iteration")
        if it is None:
            continue
        if r["type"] == "perception":
            perc_by_iter[it] = r
        elif r["type"] == "decision":
            dec_by_iter[it] = r

    iterations_html = []
    sorted_iters = sorted(set(perc_by_iter) | set(dec_by_iter))
    prev_dec = None
    for it in sorted_iters:
        perc = perc_by_iter.get(it)
        dec  = dec_by_iter.get(it)
        if perc and dec:
            iterations_html.append(build_iteration(it, perc, dec, perc_sys, dec_sys, prev_dec))
        elif perc:
            # Final perception call after all decisions done
            iterations_html.append(
                f'<div class="iter-header">── Iteration {it} (final perception — all_done check) ──</div>'
                f'<div style="padding:8px;color:#666;">Perception called to verify all goals done. '
                f'all_done={all_done}. Loop exits.</div>'
            )
        prev_dec = dec

    actor_headers = "".join(
        f'<div class="actor-hdr" style="background:{COLORS[i]};color:#fff;">{e(ACTORS[i])}</div>'
        for i in range(N_ACTORS)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PMDA Flow — Scenario {e(str(scenario))} — {e(jsonl_name)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          font-size: 13px; background: #f8f9fa; color: #1a202c; }}

  /* ── Run header ── */
  .run-header {{ background: #1a202c; color: #e2e8f0; padding: 16px 24px; }}
  .run-header h1 {{ font-size: 18px; font-weight: 700; margin-bottom: 4px; }}
  .run-header .meta {{ font-size: 12px; color: #a0aec0; }}
  .run-header .query {{ margin-top: 8px; background: #2d3748; padding: 8px 12px;
                        border-radius: 6px; font-family: monospace; color: #e2e8f0; }}

  /* ── Actor swimlane header ── */
  .actor-bar {{ display: grid; grid-template-columns: repeat(6, 1fr);
                position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }}
  .actor-hdr {{ padding: 10px 4px; text-align: center; font-weight: 700;
                font-size: 12px; letter-spacing: 0.5px; }}

  /* ── PoP validation summary ── */
  .pop-bar {{ padding: 12px 24px; display: flex; align-items: center; gap: 16px;
              border-bottom: 3px solid {pop_color}; background: {"#f0fff4" if passed else "#fff5f5"}; }}
  .pop-bar .pop-detail {{ font-size: 12px; color: #4a5568; }}

  /* ── Iteration container ── */
  .iteration {{ margin: 0; padding: 0 12px 0 12px; }}
  .iter-header {{ font-size: 11px; font-weight: 800; color: #a0aec0;
                  letter-spacing: 2px; text-transform: uppercase;
                  padding: 14px 4px 4px 4px; border-top: 1px dashed #cbd5e0; }}

  /* ── Step cards ── */
  .step-card {{ border-left: 4px solid #4a5568; border-radius: 0 8px 8px 0;
                padding: 10px 14px; margin: 6px 0; }}
  .step-title {{ font-size: 11px; font-weight: 800; text-transform: uppercase;
                 letter-spacing: 0.8px; margin-bottom: 8px; }}

  /* ── Arrow messages ── */
  .msg-arrow {{ font-size: 12px; margin: 4px 0 8px 0; white-space: nowrap;
                overflow: hidden; text-overflow: ellipsis; }}
  .from-actor {{ font-weight: 700; color: #4a5568; }}
  .to-actor   {{ font-weight: 700; }}
  .arr        {{ color: #a0aec0; font-family: monospace; }}

  /* ── Details / collapsible ── */
  .detail-body {{ padding: 8px 12px; background: rgba(255,255,255,0.7);
                  border-radius: 0 0 6px 6px; border: 1px solid rgba(0,0,0,0.06); }}
  pre.code {{ background: #1a202c; color: #e2e8f0; padding: 10px 14px;
              border-radius: 6px; font-size: 11px; white-space: pre-wrap;
              word-break: break-word; max-height: 400px; overflow-y: auto;
              font-family: "Cascadia Code", "Fira Code", Consolas, monospace; }}

  /* ── Answer box ── */
  .answer-box {{ background: #f0fff4; border: 1px solid #9ae6b4; border-radius: 6px;
                 padding: 10px 14px; font-size: 12px; white-space: pre-wrap;
                 max-height: 200px; overflow-y: auto; margin-top: 6px; }}

  /* ── Final answer ── */
  .final-section {{ margin: 16px 12px; padding: 16px; background: white;
                    border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }}
  .final-section h2 {{ font-size: 14px; font-weight: 700; margin-bottom: 8px; }}
  .final-answer {{ white-space: pre-wrap; font-size: 13px; line-height: 1.6;
                   max-height: 400px; overflow-y: auto; }}

  /* ── Expand all / collapse all button ── */
  .controls {{ padding: 8px 24px; background: #edf2f7; display: flex; gap: 8px; }}
  .btn {{ padding: 4px 12px; border-radius: 4px; border: 1px solid #cbd5e0;
          background: white; cursor: pointer; font-size: 12px; font-weight: 600; }}
  .btn:hover {{ background: #e2e8f0; }}
</style>
</head>
<body>

<!-- ── Run header ──────────────────────────────────────────────────────────── -->
<div class="run-header">
  <h1>PMDA Sequence Flow — Scenario {e(str(scenario))}</h1>
  <div class="meta">run_id: {e(run_id)} &nbsp;|&nbsp; log: {e(jsonl_name)} &nbsp;|&nbsp; timestamp: {e(str(ts))}</div>
  <div class="query">Query: {e(query)}</div>
</div>

<!-- ── PoP Validation banner ───────────────────────────────────────────────── -->
<div class="pop-bar">
  {pop_badge}
  <div class="pop-detail">
    Iterations used: <strong>{e(str(iters_used))}</strong>
    &nbsp;|&nbsp; Expected ≤ <strong>{e(str(int(iters_exp)*2 if str(iters_exp).isdigit() else iters_exp))}</strong>
    (base: {e(str(iters_exp))})
    &nbsp;|&nbsp; All goals done: <strong>{e(str(all_done))}</strong>
    &nbsp;|&nbsp; PoP = Prompt of Prompts (system prompts for each layer)
  </div>
</div>

<!-- ── Controls ───────────────────────────────────────────────────────────── -->
<div class="controls">
  <button class="btn" onclick="document.querySelectorAll('details').forEach(d=>d.open=true)">Expand All</button>
  <button class="btn" onclick="document.querySelectorAll('details').forEach(d=>d.open=false)">Collapse All</button>
  <button class="btn" onclick="document.querySelectorAll('details[data-pop]').forEach(d=>d.open=!d.open)">Toggle PoP Prompts</button>
</div>

<!-- ── Actor swimlane header ───────────────────────────────────────────────── -->
<div class="actor-bar">{actor_headers}</div>

<!-- ── Iterations ─────────────────────────────────────────────────────────── -->
<div class="iteration">
{"".join(iterations_html)}
</div>

<!-- ── Final answer + PoP JSON ────────────────────────────────────────────── -->
<div class="final-section">
  <h2 style="color:{pop_color};">Final Answer &amp; PoP Validation Record</h2>
  <div class="final-answer">{e(answer_prev)}</div>
  <details style="margin-top:12px;">
    <summary style="cursor:pointer;font-weight:600;color:{pop_color};">pop_validation JSON record</summary>
    <pre class="code">{e(json.dumps(pop, indent=2))}</pre>
  </details>
</div>

</body>
</html>"""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    jsonl_path = Path(sys.argv[1])
    if not jsonl_path.exists():
        print(f"File not found: {jsonl_path}")
        sys.exit(1)

    records  = load_jsonl(jsonl_path)
    base_dir = Path(__file__).parent
    perc_sys, dec_sys = read_source_prompts(base_dir)

    html = build_html(records, jsonl_path.name, perc_sys, dec_sys)

    # Write next to the JSONL or to console_output/
    out_dir = base_dir / "console_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (jsonl_path.stem + "_flow.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()