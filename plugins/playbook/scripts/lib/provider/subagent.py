"""The sandboxed headless subagent runner — one primitive, many call-sites.

Judge, panel, the sandbox CLI, and streaming chat sidebars are all the same
thing along four axes: which agent, what prompt, how contained, where the output
goes. This module is the composition layer that ties together the two pieces that
already exist separately:

    adapter.headless_argv(...)   # prompt → Invocation (per-provider dialect)
        ↓
    sandbox.run(...)             # containment + dispatch

It lives above both adapters and sandbox (like cli.py), so it may import both —
sandbox.py must NOT import this (adapters import sandbox, not vice versa).

Sinks:
  - text:   return stdout (judge/panel)
  - file:   harvest <workspace>/<harvest> (contained corpus reader)
  - stream: see stream_subagent() (chat sidebars) — added in Step 3.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, Optional

from . import sandbox as _sandbox


def _isolated_subagent_env(session_id: str) -> dict[str, str]:
    """Env for a sandboxed subagent that CANNOT share the foreground session.

    Without an explicit env, `sandbox.run(env=None)` builds `dict(os.environ)`,
    which PRESERVES the foreground `PLAYBOOK_SESSION_ID` — so a writable subagent
    resolves the foreground session dir and its SessionEnd hook could delete the
    foreground's active-task pointer (panel codex-sol F2, task 037). Pin an
    isolated per-invocation id; `sandbox._child_env` strips the remaining
    parent-session identity vars (CLAUDE_ENV_FILE, the CLAUDE_CODE_* siblings)."""
    env = dict(os.environ)
    env["PLAYBOOK_SESSION_ID"] = session_id
    return env


def _adapter_class(agent: str):
    """Map a canonical agent key to its adapter class. Kept here, not in
    sandbox.py, to respect the import direction (adapters import sandbox)."""
    from .adapters.claude import ClaudeAdapter
    from .adapters.codex import CodexAdapter
    from .adapters.antigravity import AntigravityAdapter
    from .adapters.grok import GrokAdapter
    from .adapters.pi import PiAdapter
    table = {
        "claude": ClaudeAdapter,
        "codex": CodexAdapter,
        "agy": AntigravityAdapter,
        "pi": PiAdapter,
        "grok": GrokAdapter,
    }
    try:
        return table[agent]
    except KeyError:
        raise ValueError(f"unknown agent {agent!r}; expected one of {sorted(table)}")


@dataclass
class SubagentSpec:
    """A provider-agnostic description of one headless subagent run."""
    agent: str                                  # claude | codex | agy | grok | pi
    model: Optional[str] = None
    prompt: str = ""
    context: str = ""                           # system context; ignored when bare
    bare: bool = False
    # Containment is always on (everything routes through sandbox.run). "repo" =
    # project writable (current default); "outdir" = corpus read-only, workspace
    # and any explicit extra_rw paths writable. There is no uncontained mode.
    contain: Literal["repo", "outdir"] = "repo"
    workspace: Optional[Path] = None            # managed output dir for contain="outdir"
    sink: Literal["text", "file"] = "text"
    harvest: str = "answer.md"                  # file to read back when sink="file"
    timeout_secs: int = 300
    # Appended for compatibility with positional construction of older fields.
    extra_rw: tuple[str, ...] = ()              # additional CLI-requested writable paths


@dataclass
class SubagentResult:
    text: str                                   # stdout (text) or harvested file (file)
    returncode: int
    raw_stdout: str = ""


def build_invocation(spec: SubagentSpec, *, project_root: Path | str,
                     stream: bool = False, session_id: str = "subagent"):
    """Build the agent's native headless invocation WITHOUT running it.

    Split out of run_subagent so `sandbox --print-argv --prompt ...` inspects the
    same argv the run would use — one source of truth, so the dry run can never
    drift from the real one.

    `session_id` names the adapter's identity; the run/stream entrypoints pass a
    unique isolated id (matching the env's `PLAYBOOK_SESSION_ID`) so a subagent
    never shares the foreground session. Defaults to the legacy "subagent" for
    the dry-run/inspection path where no run happens.
    """
    adapter = _adapter_class(spec.agent)(
        session_id=session_id, project_root=Path(project_root))
    return adapter.headless_argv(
        spec.prompt, spec.model, context=spec.context, bare=spec.bare,
        **({"stream": True} if stream else {}))


def run_subagent(spec: SubagentSpec, *, project_root: Path | str) -> SubagentResult:
    """Run a subagent to completion and return its result (text/file sinks).

    Streaming is a separate entrypoint (stream_subagent) because it can't return
    a single value — it yields events as they arrive.
    """
    project_root = Path(project_root)
    sub_sid = f"subagent-{uuid.uuid4().hex[:12]}"
    inv = build_invocation(spec, project_root=project_root, session_id=sub_sid)

    extra_rw = list(spec.extra_rw)
    if spec.workspace:
        extra_rw.append(str(spec.workspace))
    # contain="outdir": corpus read-only; explicitly named extra paths (including
    # workspace when present) remain writable. "repo" keeps the project writable.
    project_writable = spec.contain != "outdir"
    result = _sandbox.run(
        spec.agent, inv.argv,
        project_root=project_root,
        input=inv.stdin,
        env=_isolated_subagent_env(sub_sid),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=spec.timeout_secs,
        extra_rw=extra_rw or None,
        project_writable=project_writable,
    )
    stdout = result.stdout or ""

    if spec.sink == "file":
        if spec.workspace is None:
            raise ValueError("sink='file' requires a workspace to harvest from")
        target = Path(spec.workspace) / spec.harvest
        text = target.read_text(encoding="utf-8") if target.exists() else ""
    else:
        text = _sandbox.format_judge_output(result)

    return SubagentResult(text=text, returncode=result.returncode, raw_stdout=stdout)


# ── Streaming sink (chat sidebars) ───────────────────────────────────────────
# provider/events.py models hook-INPUT events (user messages, tool calls). The
# agent's OUTPUT stream (assistant token deltas for a sidebar) is a different
# shape, so it gets its own lightweight event here.
@dataclass
class StreamEvent:
    kind: Literal["text", "tool", "done", "raw"]
    text: str = ""
    tool: str = ""
    raw: Optional[dict] = None


def parse_stream_line(line: str) -> Optional[StreamEvent]:
    """Parse one line of agent stream-json output into a StreamEvent.

    Tolerant by design: blank lines → None; non-JSON → raw text; unrecognized
    JSON → raw. Handles Claude's `--output-format stream-json`
    (`stream_event`/content_block_delta partial deltas and full `assistant`
    blocks) and generic `{type: result|done}` completion. pi `--mode json`
    assistant/text shapes fall through to the assistant-content path.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return StreamEvent("raw", text=line)
    if not isinstance(obj, dict):
        return StreamEvent("raw", raw={"value": obj})

    t = obj.get("type")
    if t == "stream_event":
        ev = obj.get("event", {})
        if ev.get("type") == "content_block_delta":
            return StreamEvent("text", text=ev.get("delta", {}).get("text", ""), raw=obj)
        return StreamEvent("raw", raw=obj)
    if t == "assistant":
        content = obj.get("message", {}).get("content", obj.get("content", []))
        texts, tools = [], []
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "tool_use":
                tools.append(c.get("name", ""))
        if tools:
            return StreamEvent("tool", tool=tools[0], text="".join(texts), raw=obj)
        return StreamEvent("text", text="".join(texts), raw=obj)
    if t in ("result", "done"):
        return StreamEvent("done", raw=obj)
    return StreamEvent("raw", raw=obj)


def stream_subagent(spec: SubagentSpec, *, project_root: Path | str) -> Iterator[StreamEvent]:
    """Run a subagent and yield StreamEvents as its output arrives — the chat
    sidebar sink. Uses sandbox.popen (non-blocking) so tokens surface live.
    """
    import subprocess
    import threading
    project_root = Path(project_root)
    sub_sid = f"subagent-{uuid.uuid4().hex[:12]}"
    inv = build_invocation(spec, project_root=project_root, stream=True,
                           session_id=sub_sid)
    extra_rw = list(spec.extra_rw)
    if spec.workspace:
        extra_rw.append(str(spec.workspace))
    project_writable = spec.contain != "outdir"

    popen_kwargs = {}
    if inv.stdin is not None:           # codex feeds its prompt on stdin
        popen_kwargs["stdin"] = subprocess.PIPE
    proc = _sandbox.popen(
        spec.agent, inv.argv,
        project_root=project_root,
        env=_isolated_subagent_env(sub_sid),
        extra_rw=extra_rw or None,
        project_writable=project_writable,
        **popen_kwargs,
    )
    if inv.stdin is not None and proc.stdin is not None:
        proc.stdin.write(inv.stdin)
        proc.stdin.close()
    # Watchdog: a stuck agent must not hang the sidebar forever. On expiry, kill
    # the child — that closes stdout, so the iteration below ends naturally.
    watchdog = threading.Timer(spec.timeout_secs, proc.kill)
    watchdog.start()
    try:
        for line in proc.stdout:
            ev = parse_stream_line(line)
            if ev is not None:
                yield ev
    finally:
        watchdog.cancel()
        proc.wait()
