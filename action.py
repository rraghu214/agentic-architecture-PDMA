"""
Action layer — pure MCP dispatcher, no LLM calls.

Responsibilities:
  1. Guard against art: handles being passed as tool arguments (returns error string).
  2. Dispatch the tool call to the MCP session.
  3. If result > ARTIFACT_THRESHOLD_BYTES, store in ArtifactStore and return handle.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from mcp import ClientSession

from schemas import Artifact, ToolCall

# ── ArtifactStore ────────────────────────────────────────────────────────────

ARTIFACTS_DIR = Path("state/artifacts")
ARTIFACT_THRESHOLD_BYTES = 4096


class ArtifactStore:
    """Content-addressable file store for large tool results.

    Handle format: art:<first-16-hex-chars-of-sha256>
    Storage: two files per artifact —
      state/artifacts/<sha>.bin   — raw bytes
      state/artifacts/<sha>.json  — Artifact metadata
    """

    def put(self, blob: bytes, *, content_type: str, source: str, descriptor: str) -> str:
        sha = hashlib.sha256(blob).hexdigest()[:16]
        artifact_id = f"art:{sha}"
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        bin_path = ARTIFACTS_DIR / f"{sha}.bin"
        if not bin_path.exists():
            bin_path.write_bytes(blob)
            meta = Artifact(
                id=artifact_id,
                content_type=content_type,
                size_bytes=len(blob),
                source=source,
                descriptor=descriptor,
            )
            (ARTIFACTS_DIR / f"{sha}.json").write_text(meta.model_dump_json(), encoding="utf-8")
        return artifact_id

    def get_bytes(self, artifact_id: str) -> bytes:
        sha = artifact_id.removeprefix("art:")
        return (ARTIFACTS_DIR / f"{sha}.bin").read_bytes()

    def exists(self, artifact_id: str) -> bool:
        sha = artifact_id.removeprefix("art:")
        return (ARTIFACTS_DIR / f"{sha}.bin").exists()

    def get_meta(self, artifact_id: str) -> Artifact:
        sha = artifact_id.removeprefix("art:")
        return Artifact.model_validate_json((ARTIFACTS_DIR / f"{sha}.json").read_text(encoding="utf-8"))


artifacts = ArtifactStore()


# ── execute ──────────────────────────────────────────────────────────────────

async def execute(session: ClientSession, tool_call: ToolCall) -> tuple[str, str | None]:
    """Dispatch one tool call to the MCP session.

    Returns (result_text, artifact_id_or_None).
    Never raises — errors are returned as strings so Perception can recover.
    """
    # Guard: art: handles must never be passed as tool arguments
    for v in tool_call.arguments.values():
        if isinstance(v, str) and v.startswith("art:"):
            return (
                f"ERROR: argument contains artifact handle '{v}'. "
                "Artifact handles are internal identifiers, not file paths or URLs. "
                "Use the ATTACHED ARTIFACTS section to access artifact content.",
                None,
            )

    result = await session.call_tool(tool_call.name, arguments=tool_call.arguments)
    raw_text = " ".join(c.text for c in result.content if hasattr(c, "text"))

    size = len(raw_text.encode())
    if size > ARTIFACT_THRESHOLD_BYTES:
        art_id = artifacts.put(
            raw_text.encode(),
            content_type="text/plain",
            source=tool_call.name,
            descriptor=f"{tool_call.name}({list(tool_call.arguments.values())[:2]}) -> {size} bytes",
        )
        preview = raw_text[:200]
        return (f"[artifact {art_id}, {size} bytes] preview: {preview}", art_id)

    return (raw_text, None)
