"""Mind-map reading: loading/trimming for judge+bootstrap context, the
node-parser family, and the `mindmap-sync` command.

Boundary: everything that PARSES or BUDGETS a mind map lives here — the
context loader (`_load_mind_map` + node-aware/line-based trims + the omitted-
nodes notice) and the bootstrap INDEX loader (`_bootstrap_mind_map` +
`_mind_map_toc` — routing nodes in full plus a titled TOC of the rest, so
orientation costs an index not a full dump), the fence-aware node-boundary family (`_node_starts` is THE
shared detector; `_partition_overflow` / `sort_overflow_by_id` /
`_scan_overflow_ids` / the unnumbered-tail notice build on it), `_parse_nodes`
(the byte-exact collision-detector parser consumed by prepare-merge), and the
`mindmap-sync` arm. Imports stdlib + tasks.shared only (leaf order:
shared < mindmap < command modules — design-1.5.9.md §4 ⟦C4⟧); never a
command module. Consumers: review/bootstrap (context), merge_prep
(`_parse_nodes`), the dispatcher.
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from tasks.shared import find_project_root


def _omitted_nodes_notice(omitted_ids: list[int], total: int, *,
                          first_truncated: bool = False,
                          preamble_dropped: bool = False) -> str:
    """The in-band notice that names which mind-map nodes did NOT make the budget.

    Load-bearing, not decoration: every judge prompt asserts "the MIND_MAP.md is
    provided" (`template.py`), so a silently-trimmed map reads as a complete one
    and the judge never goes looking for what is missing. Naming the ids — and the
    grep that fetches them — is what turns a silent loss into a recoverable one.
    Every kind of loss this trim can inflict has to be speakable here, or it is
    back to being invisible.
    """
    if not omitted_ids and not first_truncated and not preamble_dropped:
        return ""
    parts = []
    if omitted_ids:
        ids = ", ".join(f"[{n}]" for n in omitted_ids)
        parts.append(
            f"[... MIND MAP TRIMMED to fit the context budget: "
            f"{len(omitted_ids)} of {total} nodes omitted — {ids}. "
            f"This is NOT the full map. Read any omitted node with: "
            f"grep '^\\[N\\]' MIND_MAP.md ...]"
        )
    if preamble_dropped:
        parts.append(
            "[... the map's header (editing rules, ownership map) was dropped to make "
            "room for the routing node. Read the top of MIND_MAP.md for it ...]"
        )
    if first_truncated:
        parts.append(
            "[... the node below is itself CUT MID-NODE — the budget could not hold "
            "even one whole node. Read MIND_MAP.md directly ...]"
        )
    return "\n".join(parts) + "\n\n"


def _trim_mind_map_by_node(content: str, max_chars: int) -> str | None:
    """Node-aware trim: drop WHOLE nodes to fit, and name the ones dropped.

    Returns None when `content` has no usable node markers, so the caller can fall
    back to the line-based trim (a mind map is not required to be node-shaped).

    Why not the line-based trim: mind-map nodes are conventionally ONE long line
    each (`^[N] **Title** - prose…`), so cutting on a line boundary at 60%/40%
    does not shed prose evenly — it sheds entire subsystem chapters, and says only
    "N lines omitted". A 120 KB / 19-node map delivered nodes [0], [17], [18] and
    nothing else. Whole nodes in, named ids for the rest, is strictly more useful
    at the same byte cost.

    Selection is file order, first node first. That drops the old trim's 60/40
    head+tail bias deliberately: its stated reason was "the tail has recent
    additions and roadmap", which does not hold for the real node order — Status
    and Roadmap are head nodes ([4], [5]), and the tail is simply the
    newest-numbered SUBSYSTEMS. File order keeps the routing nodes the map's own
    header tells readers to start at, and named omissions replace what the tail
    bias was trying to buy. Every node that fits is taken, so a later short `↗`
    node still lands even when an earlier fat one did not. The first node is kept
    unconditionally (truncated only if the budget cannot hold it) because it is the
    routing/overview node the rest links to.
    """
    lines = content.splitlines(keepends=True)
    starts, in_fence = _node_starts(lines)   # shared fence-aware scan
    if in_fence or not starts:
        return None

    preamble = "".join(lines[: starts[0][0]])
    if preamble and not preamble.endswith("\n"):
        preamble += "\n"         # before budgeting, so the fix cannot overrun it
    spans: list[tuple[int, str]] = []
    for k, (idx, nid) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        spans.append((nid, "".join(lines[idx:end])))
    total = len(spans)

    # Reserve the worst-case notice (every id named, plus both extra lines) so the
    # selection below can never be invalidated by the notice it will grow.
    reserve = len(_omitted_nodes_notice(
        [nid for nid, _ in spans], total, first_truncated=True, preamble_dropped=True))
    budget = max(max_chars - reserve, 0)

    first_text = spans[0][1]
    preamble_dropped = False
    if preamble and len(preamble) + len(first_text) > budget:
        preamble = ""            # the routing node outranks the header prose
        preamble_dropped = True
    first_truncated = len(first_text) > budget
    if first_truncated:
        first_text = first_text[:budget]

    kept = {0: first_text}
    used = len(preamble) + len(first_text)
    if not first_truncated:
        for i in range(1, total):
            text = spans[i][1]
            if used + len(text) <= budget:
                kept[i] = text
                used += len(text)

    omitted = [nid for i, (nid, _) in enumerate(spans) if i not in kept]
    notice = _omitted_nodes_notice(omitted, total, first_truncated=first_truncated,
                                   preamble_dropped=preamble_dropped)
    result = preamble + notice + "".join(kept[i] for i in sorted(kept))
    return result[:max_chars]


def _trim_mind_map_by_lines(content: str, max_chars: int) -> str:
    """Line-based head+tail trim — the fallback for maps with no node markers.

    Head has overview nodes [1]-[4]; tail has recent additions and roadmap.
    The middle is the most expendable, so we trim there on a line boundary.
    """
    max_omitted_digits = len(str(content.count("\n")))
    marker_budget = len(f"\n\n[... {'9' * max_omitted_digits} lines omitted ...]\n")
    available = max(max_chars - marker_budget, 0)
    if available == 0:
        return content[:max_chars]

    # Keep 60% head, 40% tail — overview nodes are denser at the top.
    head_budget = int(available * 0.6)
    tail_budget = available - head_budget

    # Snap inward to line boundaries so the head/tail stay within budget.
    head_end = content.rfind("\n", 0, head_budget)
    if head_end < 0:
        head_end = head_budget
    tail_start = content.find("\n", len(content) - tail_budget)
    if tail_start < 0:
        tail_start = len(content) - tail_budget
    else:
        tail_start += 1
    head = content[:head_end]
    tail = content[tail_start:]
    omitted = content[head_end:tail_start].count("\n")
    marker = f"\n\n[... {omitted} lines omitted ...]\n"
    result = f"{head}{marker}{tail}"
    if len(result) > max_chars:
        overflow = len(result) - max_chars
        if overflow < len(tail):
            tail = tail[overflow:]
        else:
            head = head[:max(len(head) - (overflow - len(tail)), 0)]
            tail = ""
        result = f"{head}{marker}{tail}"
    return result[:max_chars]


def _load_mind_map(project_path: Path, max_chars: int = 25000) -> str | None:
    """Load MIND_MAP.md content, trimmed to max_chars if it is over budget.

    Trimming is node-aware (`_trim_mind_map_by_node`): whole `^[N]` nodes are
    dropped to fit and the dropped ids are NAMED in the returned text, so the
    reader knows what is missing and can grep for it. A map with no node markers
    falls back to the older line-based head+tail trim.

    Set PLAYBOOK_MINDMAP_MAX env var to override max_chars (0 or less = suppress
    entirely).
    """
    env_max = os.environ.get("PLAYBOOK_MINDMAP_MAX")
    if env_max is not None:
        max_chars = int(env_max)
        if max_chars <= 0:
            return None
    mind_map = project_path / "MIND_MAP.md"
    if not mind_map.exists():
        return None
    content = mind_map.read_text(encoding="utf-8")
    if len(content) <= max_chars:
        return content
    by_node = _trim_mind_map_by_node(content, max_chars)
    if by_node is not None:
        return by_node
    return _trim_mind_map_by_lines(content, max_chars)


_FENCE_RE = re.compile(r"^\s*```")
_NODE_HEAD_RE = re.compile(r"^\[(\d+)\]")


def _node_starts(lines: list[str]) -> tuple[list[tuple[int, int]], bool]:
    """Fence-aware scan for node-definition lines — the ONE shared node-boundary
    detector behind `_partition_overflow`, `_scan_overflow_ids`, and the
    `mindmap-sync` `_extract_nodes`. All three agree on node STARTS because they
    share this scan (body extent is each caller's own concern).

    `lines` is `content.splitlines(keepends=True)`. Returns
    `(starts, in_fence_at_eof)` where `starts = [(line_index, node_id)]` for every
    `^[N]` line that is NOT inside a ``` code fence — so a fenced `[9]` example is
    never a ghost node. An unmatched fence surfaces as `in_fence_at_eof=True` so
    callers can fail closed.
    """
    starts: list[tuple[int, int]] = []
    in_fence = False
    for i, ln in enumerate(lines):
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            continue
        if not in_fence:
            m = _NODE_HEAD_RE.match(ln)
            if m:
                starts.append((i, int(m.group(1))))
    return starts, in_fence


_NODE_TITLE_RE = re.compile(r"^\[(\d+)\]\s*(.*)")
_BOLD_TITLE_RE = re.compile(r"\*\*(.+?)\*\*")

# Bootstrap-only mind-map budget. Deliberately much smaller than
# `_load_mind_map`'s 25000: bootstrap is ORIENTATION (the agent needs the shape
# plus the entry nodes, then greps the 2-3 nodes its task touches), so a full
# dump is resident cost paid every session for context the task never reads. A
# map under this many chars is cheap enough to dump whole — the retrieval
# round-trips an index would force are not worth saving a few hundred tokens.
_BOOTSTRAP_MINDMAP_BUDGET = 8000


def _node_title(line: str) -> tuple[int, str]:
    """`(node_id, title)` for one `^[N]` node-definition line.

    Title is the bold `**…**` when present (the format the /mindmap skill
    mandates); otherwise the text up to the ` - ` body separator, capped, so a
    node that skipped the bold convention still indexes as something legible
    rather than a bare id.
    """
    m = _NODE_TITLE_RE.match(line.rstrip("\n"))
    if m is None:                       # unreachable via _node_starts; defensive
        return (-1, line.strip()[:60] or "node")
    nid = int(m.group(1))
    rest = m.group(2).strip()
    bold = _BOLD_TITLE_RE.match(rest)
    if bold:
        title = bold.group(1).strip()
    else:
        title = rest.split(" - ", 1)[0].strip()[:60]
    return nid, (title or f"node {nid}")


def _mind_map_toc(content: str) -> str | None:
    """One `[N] Title` line per node — the bootstrap INDEX of a mind map.

    Fence-aware (shares `_node_starts`), so a `[9]` inside a code fence is an
    example, never a phantom TOC entry. Returns None when the content has no
    usable node markers (an open fence or none at all) so the caller keeps the
    full text instead of emitting an empty index.

    The titles are the whole point: `_omitted_nodes_notice` can only name dropped
    ids (`[47]`), which tells a reader something is missing but not WHICH node to
    fetch. A titled line (`[47] Sandbox containment`) turns "grep blindly" into
    "grep the one node this task touches" — the difference between an index and a
    hole.
    """
    lines = content.splitlines(keepends=True)
    starts, in_fence = _node_starts(lines)
    if in_fence or not starts:
        return None
    return "\n".join(f"[{nid}] {_node_title(lines[idx])[1]}"
                     for idx, nid in starts)


def _bootstrap_mind_map(project_path: Path,
                        budget_chars: int = _BOOTSTRAP_MINDMAP_BUDGET,
                        routing: int = 5) -> str | None:
    """Bootstrap's mind-map loader: full text when small, else an INDEX.

    Distinct from `_load_mind_map` (the judge/review path) ON PURPOSE. A judge is
    AUDITING and may need many whole nodes at once, so that path keeps the 25k
    whole-node trim. Bootstrap is ORIENTING a fresh agent, which needs the map's
    SHAPE and its entry nodes, then greps the handful its task touches — so over
    budget it returns the first `routing` nodes in full (the overview/routing
    hubs the header says to read first) plus a TITLED one-line TOC of every other
    node and the grep that fetches them. Resident cost drops from up-to-25k of
    prose to a few routing nodes plus one line per subsystem.

    Honours `PLAYBOOK_MINDMAP_MAX <= 0` as the global "no mind-map context"
    suppression (same escape hatch as `_load_mind_map`); a positive value there
    tunes the JUDGE budget only and does not change bootstrap's index threshold.

    Falls back to `_load_mind_map` (the whole-node/line trim) when the map is not
    node-shaped enough to index — no nodes, an open fence, or fewer nodes than
    `routing` (a handful of nodes IS the overview; there is nothing to index).
    """
    env_max = os.environ.get("PLAYBOOK_MINDMAP_MAX")
    if env_max is not None:
        try:
            if int(env_max) <= 0:
                return None
        except ValueError:
            pass
    mind_map = project_path / "MIND_MAP.md"
    if not mind_map.exists():
        return None
    content = mind_map.read_text(encoding="utf-8")
    if len(content) <= budget_chars:
        return content

    lines = content.splitlines(keepends=True)
    starts, in_fence = _node_starts(lines)
    if in_fence or len(starts) <= routing:
        return _load_mind_map(project_path)

    preamble = "".join(lines[: starts[0][0]])
    if preamble and not preamble.endswith("\n"):
        preamble += "\n"
    routed = "".join(lines[starts[0][0]: starts[routing][0]])
    first_id, last_id = starts[0][1], starts[routing - 1][1]
    indexed = starts[routing:]
    toc = "\n".join(f"[{nid}] {_node_title(lines[idx])[1]}" for idx, nid in indexed)
    has_overflow = (project_path / "MIND_MAP_OVERFLOW.md").exists()
    fetch = ("`tasks recall <N>` (spans MIND_MAP.md + the fuller MIND_MAP_OVERFLOW.md)"
             if has_overflow else "`tasks recall <N>` or grep '^\\[N\\]' MIND_MAP.md")
    n_indexed = len(indexed)
    node_word = "node" if n_indexed == 1 else "nodes"
    notice = (
        f"[... MIND MAP INDEX — routing nodes [{first_id}]-[{last_id}] shown in "
        f"full above; the {n_indexed} {node_word} below {'is' if n_indexed == 1 else 'are'} "
        f"listed by TITLE ONLY. This is NOT their content. Fetch any one with: {fetch}. "
        f"Locate a node by topic: `tasks recall <keyword>` ...]\n\n"
    )
    return f"{preamble}{routed}\n{notice}{toc}\n"


def _partition_overflow(content: str):
    """Fence-aware partition of an OVERFLOW file into (preamble, spans, tail).

    `spans` is `[(node_id, raw_span_text)]` in file order, and
    `preamble + ''.join(raw) + tail == content` byte-for-byte. Returns **None**
    (caller fails closed) when the structure is ambiguous or unsafe to reorder:
    an unmatched code fence, no nodes, a blank-line-preceded markdown heading
    inside a NON-last node (can't tell node content from a section), or a coverage
    miss.

    `tail` = a trailing non-node section (e.g. `## Legacy`) detached off the LAST
    span — recognized ONLY at a heading **preceded by a blank line**, so a `## …`
    line glued directly to node prose stays part of that node (not amputated).
    """
    fence_re = _FENCE_RE
    heading_re = re.compile(r"^#{1,6}\s")

    lines = content.splitlines(keepends=True)
    starts, in_fence = _node_starts(lines)   # shared fence-aware scan
    if in_fence or not starts:
        return None

    preamble = "".join(lines[: starts[0][0]])
    spans: list[tuple[int, str]] = []
    for k, (idx, nid) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        spans.append((nid, "".join(lines[idx:end])))

    def _section_heading_idx(span_text: str):
        """Line index of the first blank-line-preceded heading (fence-aware), or None."""
        sl = span_text.splitlines(keepends=True)
        infence = False
        for j in range(1, len(sl)):
            if fence_re.match(sl[j]):
                infence = not infence
                continue
            if not infence and heading_re.match(sl[j]) and sl[j - 1].strip() == "":
                return j
        return None

    # A section heading inside a non-last node is ambiguous — refuse to reorder.
    for _, span in spans[:-1]:
        if _section_heading_idx(span) is not None:
            return None

    # Detach a trailing section off the LAST span (blank-preceded heading only).
    tail = ""
    last_nid, last_span = spans[-1]
    j = _section_heading_idx(last_span)
    if j is not None:
        sl = last_span.splitlines(keepends=True)
        spans[-1] = (last_nid, "".join(sl[:j]))
        tail = "".join(sl[j:])

    if preamble + "".join(s for _, s in spans) + tail != content:
        return None
    return (preamble, spans, tail)


def sort_overflow_by_id(content: str) -> tuple[str, bool, str]:
    """Sort MIND_MAP_OVERFLOW.md `[N]` nodes into ascending numeric order.

    Pure + fail-safe. Returns (new_content, changed, reason).

    Contract:
    - **Already sorted / unsortable / ambiguous → (content, False, reason)**: input
      returned byte-for-byte; the caller must NOT rewrite (a sorted CRLF file is
      never normalized).
    - **Reordered → (new_content, True, "reordered N node(s)")**: node *bodies* and
      the preamble + trailing section are preserved byte-for-byte; only the blank-line
      separators BETWEEN nodes are canonicalized to the file's dominant separator.
      Idempotent — re-running is a no-op.

    Safety: fence-aware (a `[N]` line inside a ``` fence is not a node start); the
    reordered output is re-parsed and must yield the same preamble, the same node-body
    multiset, the same tail, and ascending ids — else it fails closed.
    """
    parsed = _partition_overflow(content)
    if parsed is None:
        return (content, False, "ambiguous/unparseable structure — left unchanged")
    preamble, spans, tail = parsed
    if len(spans) < 2:
        return (content, False, "fewer than 2 nodes — nothing to sort")

    ids = [nid for nid, _ in spans]
    if ids == sorted(ids):
        return (content, False, "already sorted")

    sep = "\r\n\r\n" if "\r\n" in content else "\n\n"
    ordered = sorted(spans, key=lambda t: t[0])        # stable: dup ids keep order
    bodies = [s.rstrip("\r\n") for _, s in ordered]    # node text, sans trailing sep
    new_content = sep.join(bodies)
    if preamble:                                        # preserve preamble byte-exact
        glue = "" if preamble.endswith(("\n", "\r")) else sep
        new_content = preamble + glue + new_content
    if tail:                                            # preserve tail byte-exact
        new_content = new_content + sep + tail
    if content.endswith("\r\n") and not new_content.endswith("\r\n"):
        new_content += "\r\n"
    elif content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"

    # REAL post-check: re-parse the output and compare structurally. Fail closed on
    # any mismatch (catches separator collisions, lost bytes, mis-detached sections).
    reparsed = _partition_overflow(new_content)
    if reparsed is None:
        return (content, False, "reordered output failed re-parse — left unchanged")
    new_pre, new_spans, new_tail = reparsed
    new_ids = [nid for nid, _ in new_spans]
    if (new_pre == preamble and new_tail == tail and new_ids == sorted(ids)
            and sorted(s.rstrip("\r\n") for _, s in new_spans) == sorted(bodies)):
        return (new_content, True, f"reordered {len(ids)} node(s)")
    return (content, False, "post-sort verification failed — left unchanged")


def _scan_overflow_ids(content: str) -> tuple[list[int], bool, bool]:
    """Fence-aware node-id scan. Returns (ids_in_file_order, in_fence_at_eof, ok)."""
    starts, in_fence = _node_starts(content.splitlines(keepends=True))
    return ([nid for _, nid in starts], in_fence, not in_fence)


_HEADING_RE = re.compile(r"^#{1,6}\s")


def _unnumbered_tail(content: str) -> str:
    r"""Return the trailing unnumbered section after the LAST numbered `[N]` node —
    i.e. the bytes `_extract_nodes` trims off the last node at its first markdown
    heading — or "" if there is none.

    This mirrors `_extract_nodes`' heading-trim (first non-fenced `^#{1,6}\s`
    heading after the node's first line) applied to the LAST node's span, so the
    notice and the drift diagnostics AGREE on what counts as node body. It catches
    a `## Legacy`/scaffolding block whether the heading is blank-line-preceded OR
    glued directly to the last node's prose. It deliberately does NOT use
    `_partition_overflow` (whose `tail` only detaches at a blank-preceded heading),
    so it never touches the `--fix` fail-closed path. Heading-led only: trailing
    prose with no heading is indistinguishable from the node's own body and is not
    reported. Read-only and side-effect-free."""
    lines = content.splitlines(keepends=True)
    starts, _ = _node_starts(lines)
    if not starts:
        return ""
    span = lines[starts[-1][0]:]          # last [N] line → EOF
    in_fence = False
    for i in range(1, len(span)):
        if _FENCE_RE.match(span[i]):
            in_fence = not in_fence
            continue
        if not in_fence and _HEADING_RE.match(span[i]):
            return "".join(span[i:])      # heading + everything after, byte-exact
    return ""


_DATE_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEEPWORD_RE = re.compile(r"\b(?:keep|kept)\b", re.IGNORECASE)


def _is_keepnote_line(line: str) -> bool:
    """A DELIBERATE keep-acknowledgement: the WHOLE WORD keep/kept AND a YYYY-MM-DD
    date on the SAME line (e.g. "## Legacy (kept 2026-06-30)"). Requiring both on one
    line distinguishes a conscious keep-note from an INCIDENTAL date in stale prose
    (the round-3 block's "Created 2026-05-14" sits on a line with no keep/kept word).
    The keep word is matched at WORD BOUNDARIES so "bookkeeping"/"timekeeping"/
    "housekeeping"/"beekeeper" + a date do NOT false-suppress the notice."""
    return _KEEPWORD_RE.search(line) is not None and _DATE_TOKEN_RE.search(line) is not None


def _unnumbered_tail_notice(content: str) -> str:
    """Operator-facing notice for a stale heading-led unnumbered tail (see
    `_unnumbered_tail`), or "" when there is none OR it carries a deliberate dated
    keep-note (gotcha #7's "keep with a dated note", which silences this to avoid
    cry-wolf). The keep-note must be the word keep/kept AND a YYYY-MM-DD date on the
    SAME line — an INCIDENTAL date in stale prose (the round-3 block's "Created
    2026-05-14") must NOT suppress the notice. Module-level on purpose: the CLI
    `main()` has a local `import re` in another command branch, so a bare `re` there
    is an unbound local — keeping regex use out here avoids that and is testable."""
    tail = _unnumbered_tail(content)
    if not tail:
        return ""
    if any(_is_keepnote_line(ln) for ln in tail.splitlines()):
        return ""
    n = tail.strip("\r\n").count("\n") + 1
    return (f"Note: {n} unnumbered line(s) after the last numbered node "
            "(heading-led, e.g. ## Legacy) — review: remove the stale section, or "
            "keep it with a dated note (`kept YYYY-MM-DD`) to acknowledge & silence.")


def _parse_nodes(text: str) -> dict[int, str]:
    """node_id -> full raw body, for the git-merge collision detector
    (`_prepare_merge_mindmap`). Distinct from `_extract_nodes`/`_node_bodies`: it
    accumulates EVERY line of a node verbatim (no heading-trim) and requires a
    trailing space after `[N]` — its bodies feed an md5 comparison, so byte-exact
    accumulation is the point.

    Fence-aware (task 007): a `[N] ` line INSIDE a ``` code fence is NOT a node
    start — otherwise a fenced example would ghost-split the enclosing node and
    mis-attribute its body. Node-START detection uses the same fence toggle as
    `_node_starts`; the fence line itself is kept in the current node's body.
    Hoisted to module level (was nested) so it is unit-testable.
    """
    nodes: dict[int, str] = {}
    current_id: int | None = None
    current_lines: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if current_id is not None:
                current_lines.append(line)
            continue
        m = None if in_fence else re.match(r"^\[(\d+)\] ", line)
        if m:
            if current_id is not None:
                nodes[current_id] = "".join(current_lines)
            current_id = int(m.group(1))
            current_lines = [line]
        elif current_id is not None:
            current_lines.append(line)
    if current_id is not None and current_lines:
        nodes[current_id] = "".join(current_lines)
    return nodes


def cmd_mindmap_sync(cmd_args):
    """The `tasks mindmap-sync` arm — body moved verbatim from cli.py (1.5.9 split)."""
    import re as _re
    project_path = find_project_root()
    main_file = project_path / "MIND_MAP.md"
    overflow_file = project_path / "MIND_MAP_OVERFLOW.md"

    if not main_file.exists():
        print("Error: MIND_MAP.md not found", file=sys.stderr)
        sys.exit(1)
    if not overflow_file.exists():
        # F23 (genesis gauntlet): a young project has no overflow yet —
        # the merge skill's Step 6 says "mindmap-sync, then --fix", and a
        # hard error there strands a faithful run on exactly the
        # single-map shape every project starts with. No overflow means
        # nothing to mirror: say so and exit clean, the same graceful
        # degrade ref-integrity.py already ships ("overflow checks
        # skipped"). Creating one is NOT this command's job — overflow
        # exists only once the map outgrows its budget.
        print("note: MIND_MAP_OVERFLOW.md not present — nothing to sync "
              "(a single-map project is the normal young shape; overflow "
              "is created when the map outgrows its budget)")
        sys.exit(0)

    fix_mode = "--fix" in cmd_args

    def _extract_nodes(filepath: Path) -> dict[int, str]:
        """Extract {node_id: full_text} from a mind map file.

        Node STARTS come from the shared fence-aware `_node_starts` scan, so a
        `[N]` line inside a ``` fence is NOT a node boundary (it stays part of
        the enclosing node) — the three mind-map parsers agree on starts.

        The LAST node would otherwise absorb everything to EOF, so a trailing
        `## Legacy`/notes section is cut at the first markdown heading (`#…`)
        after the node's first line — but NOT a `#` line inside a fenced code
        block (a `# comment` in a code example), so multi-line overflow node
        bodies with code aren't truncated.
        """
        content = filepath.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        starts, _ = _node_starts(lines)   # fence-aware: a fenced [N] is not a node
        nodes: dict[int, str] = {}
        for k, (idx, nid) in enumerate(starts):
            end_idx = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
            part = ''.join(lines[idx:end_idx])
            plines = part.split('\n')
            end = len(plines)
            in_fence = False
            for i in range(1, len(plines)):
                if plines[i].lstrip().startswith('```'):
                    in_fence = not in_fence
                    continue
                if not in_fence and _re.match(r'^#{1,6}\s', plines[i]):
                    end = i
                    break
            nodes[nid] = '\n'.join(plines[:end]).strip()
        return nodes

    main_nodes = _extract_nodes(main_file)
    overflow_nodes = _extract_nodes(overflow_file)

    # Guard: an unmatched code fence in MIND_MAP.md makes `_extract_nodes`'
    # node boundaries (and thus every drift/missing diagnostic below AND the
    # sync source of truth) unreliable. Detect it up front — before any
    # diagnostic is printed or any --fix path (incl. sort-only) writes — so the
    # operator isn't handed a misleading "Missing from overflow" list computed
    # from a corrupt parse. Hard-stop under --fix; warn in read-only mode.
    _, _main_in_fence = _node_starts(
        main_file.read_text(encoding="utf-8").splitlines(keepends=True))
    if _main_in_fence:
        msg = ("MIND_MAP.md has an unmatched code fence — its node boundaries "
               "can't be trusted")
        if fix_mode:
            print(f"Error: {msg}, so --fix won't run. File(s) NOT modified; "
                  "close the fence and re-run.", file=sys.stderr)
            sys.exit(1)
        print(f"Warning: {msg}; the counts below may be wrong.", file=sys.stderr)

    # Size stats
    main_size = main_file.stat().st_size
    overflow_size = overflow_file.stat().st_size
    full_count = sum(1 for nid in main_nodes if '↗' not in main_nodes[nid])
    summary_count = len(main_nodes) - full_count
    print(f"MIND_MAP.md: {main_size:,} chars (~{main_size // 4:,} tokens), "
          f"{len(main_nodes)} nodes ({full_count} full, {summary_count} summary/↗)")
    print(f"MIND_MAP_OVERFLOW.md: {overflow_size:,} chars, {len(overflow_nodes)} nodes")
    print()

    # Missing nodes
    main_only = sorted(set(main_nodes) - set(overflow_nodes))
    overflow_only = sorted(set(overflow_nodes) - set(main_nodes))
    if main_only:
        print(f"Missing from overflow: {main_only}")
    if overflow_only:
        print(f"Missing from main: {overflow_only}")

    # Content drift (full nodes only — summary nodes are intentionally shorter).
    # `drifted` = EVERY full node whose main/overflow text differs, regardless
    # of length sign. A same-length ref remap (e.g. [29]→[36]) has diff==0 and
    # used to be mis-bucketed as "overflow ahead" and skipped by --fix; it now
    # lands in `drifted` and is auto-syncable. main is the canonical source, so
    # all drift syncs main→overflow.
    # NOTE: the `'↗' not in main_text` gate means SUMMARY (↗) nodes never enter
    # this comparison — so mindmap-sync structurally CANNOT catch a stale ref
    # buried in the OVERFLOW body of a ↗-summary node (the §4.3 case). That is
    # ref-integrity.py's job (whole-file ref scan) + the skill's manual grep.
    drifted: list[tuple[int, int]] = []   # (nid, signed diff = len(main)-len(overflow))
    for nid in sorted(set(main_nodes) & set(overflow_nodes)):
        main_text = main_nodes[nid]
        overflow_text = overflow_nodes[nid]
        if '↗' not in main_text and main_text != overflow_text:
            drifted.append((nid, len(main_text) - len(overflow_text)))

    if drifted:
        print("Content drift (full nodes only):")
        for nid, diff in drifted:
            if diff > 0:
                print(f"  [{nid}] main AHEAD by {diff} chars")
            elif diff < 0:
                print(f"  [{nid}] overflow AHEAD by {-diff} chars")
            else:
                print(f"  [{nid}] differs (same length — e.g. ref remap)")
    else:
        print("No content drift.")

    # Cross-reference health
    all_main_text = main_file.read_text(encoding="utf-8")
    all_refs = set(int(m.group(1)) for m in _re.finditer(r'\[(\d+)\]', all_main_text))
    broken = sorted(all_refs - set(main_nodes))
    if broken:
        print(f"\nBroken cross-references: {broken}")

    # Numeric-sort status, computed independently of drift/main_only so a
    # complete-but-out-of-order overflow (the run-2 manual-reorder case) is
    # reachable by --fix. Read newline-preserving (Path.open, not read_text's
    # newline= kwarg which is Python ≥3.13 only) so a sorted CRLF file isn't
    # flagged/rewritten.  NB: the sort path below is CRLF-safe; the drift/append
    # branch still normalizes CRLF→LF via read_text (pre-existing — Parked P1).
    with overflow_file.open(encoding="utf-8", newline="") as _f:
        overflow_raw = _f.read()
    _, sort_needed, sort_reason = sort_overflow_by_id(overflow_raw)
    if sort_needed:
        print(f"Overflow node order: out of numeric order → --fix will sort.")
    elif sort_reason not in ("already sorted", "fewer than 2 nodes — nothing to sort"):
        print(f"Overflow sort: skipped ({sort_reason}).")

    # Unnumbered-tail notice (read-only, both modes): a heading-led section after
    # the last numbered node (e.g. a stale `## Legacy` block) is invisible to the
    # numbered-node diagnostics above AND to ref-integrity (id-keyed), so a
    # faithful merge can silently retain it. Surface it for a conscious decision —
    # but NEVER auto-delete (gotcha #7 permits keeping archive content; the
    # detector is read-only and never touches the --fix write/fail-closed path).
    # Computed before the --fix block so it fires in both modes; the helper is
    # silent when there's no tail or it carries a dated keep-note (anti-cry-wolf).
    _notice = _unnumbered_tail_notice(overflow_raw)
    if _notice:
        print(f"\n{_notice}")

    # --fix: copy main→overflow for EVERY drifted full node (any length sign,
    # incl. same-length ref remaps) plus nodes missing from overflow, THEN
    # numerically sort the result (idempotent — appended nodes land in place).
    #
    # The drift edit is a span-SCOPED replace keyed by node id (via the
    # fence-aware `_partition_overflow`): for each drifted node, ONLY that
    # node's body substring is swapped INSIDE its own raw span (count=1), so
    # (a) one node's text being a substring of another can't cause a
    # wrong-occurrence hit, (b) untouched spans stay byte-identical (separators
    # and all), and (c) any post-body remainder of a drifted node (e.g. a glued
    # `## heading` + content that `_extract_nodes` truncates away) is preserved
    # rather than dropped. It is CRLF-safe end to end: it reuses the
    # newline-preserving `overflow_raw` (no `read_text` LF-normalization), and
    # converts both the old and new node text (LF, from `read_text`) to the
    # overflow's native newline before matching/splicing. `main_only` nodes are
    # appended at the boundary, each emitting its OWN `sep` (NOT relying on the
    # trailing sort, which returns bytes unchanged when ids are already
    # ascending); interior separators of existing nodes are left untouched.
    if fix_mode and (drifted or main_only or sort_needed):
        if drifted or main_only:
            # (MIND_MAP.md's fence integrity was already verified up front,
            # before any diagnostic or write — see the guard after extraction.)
            parsed = _partition_overflow(overflow_raw)
            if parsed is None:
                # Fail closed: an ambiguous structure (unmatched code fence, a
                # section heading inside a non-last node, or no nodes) can't be
                # edited span-by-span safely. Write NOTHING and exit non-zero —
                # never fall through to the old corrupting replace()/rstrip().
                print("Error: MIND_MAP_OVERFLOW.md has an ambiguous structure "
                      "(unmatched code fence, or a section heading inside a "
                      "non-last node) — --fix cannot safely edit raw spans. "
                      "File NOT modified; resolve the structure by hand and "
                      "re-run.", file=sys.stderr)
                sys.exit(1)
            preamble, spans, tail = parsed
            nl = "\r\n" if "\r\n" in overflow_raw else "\n"
            sep = nl + nl
            drift_ids = {nid for nid, _ in drifted}
            # Drift: swap ONLY the drifted node's body inside its own span,
            # preserving every other byte (untouched spans + drifted remainder).
            # The old/new text is converted to the SPAN's own newline style
            # (not the file's dominant one) so a stray CRLF elsewhere in an
            # otherwise-LF file can't make `old` un-matchable and trip a
            # spurious fail-closed on a node that's actually fine.
            out_spans = []
            for nid, span in spans:
                if nid in drift_ids:
                    span_nl = "\r\n" if "\r\n" in span else "\n"
                    old = overflow_nodes[nid].replace("\n", span_nl)
                    new = main_nodes[nid].replace("\n", span_nl)
                    if old not in span:
                        print(f"Error: could not locate node [{nid}]'s current "
                              "body within its span for an exact sync — --fix "
                              "aborted, file NOT modified.", file=sys.stderr)
                        sys.exit(1)
                    out_spans.append(span.replace(old, new, 1))
                else:
                    out_spans.append(span)               # byte-identical
            overflow_content = preamble + "".join(out_spans)
            # Append main_only nodes at the boundary only (interior separators
            # of existing nodes untouched): trim the last span's trailing
            # newlines, re-add a canonical sep before each new node, and a sep
            # before the trailing section if one exists.
            if main_only:
                overflow_content = overflow_content.rstrip("\r\n")
                for nid in main_only:
                    overflow_content += sep + main_nodes[nid].replace("\n", nl)
                if tail:
                    overflow_content += sep
            overflow_content = overflow_content + tail
            if overflow_raw.endswith("\r\n") and not overflow_content.endswith("\r\n"):
                overflow_content += "\r\n"
            elif overflow_raw.endswith("\n") and not overflow_content.endswith("\n"):
                overflow_content += "\n"
            fixed = len(drifted) + len(main_only)
        else:
            overflow_content = overflow_raw   # sort-only: preserve newlines
            fixed = 0
        overflow_content, sort_changed, sort_msg = sort_overflow_by_id(overflow_content)
        with overflow_file.open("w", encoding="utf-8", newline="") as _f:
            _f.write(overflow_content)   # newline-preserving write (3.8-safe)
        done = []
        if fixed:
            done.append(f"synced {fixed} node(s) main→overflow")
        if sort_changed:
            done.append(sort_msg)   # "reordered N node(s)"
        print(f"\nFixed: {', '.join(done) if done else 'no change needed'}")
    elif drifted or main_only:
        fixable = len(drifted) + len(main_only)
        print(f"\n{fixable} node(s) can be auto-synced main→overflow. Run: tasks mindmap-sync --fix")


# ─── Retrieval: `tasks recall` ────────────────────────────────────────────────
# The completion of the bootstrap INDEX: the index (routing nodes + titled TOC)
# tells an agent WHICH node it wants; `recall` FETCHES it — across BOTH tiers.
# A two-tier map (MIND_MAP.md holds full + `↗` summary nodes, MIND_MAP_OVERFLOW.md
# holds the deep detail) otherwise forces the agent to know that a summarized node
# has a fuller twin in a second file and to grep it by hand. `recall <N>` pulls
# both; `recall <keyword>` locates the node ids to pull. This is "know exactly
# where to look, load exactly what you need" as one command.

def _iter_map_nodes(content: str) -> "list[tuple[int, str, str]]":
    """`(node_id, title, full_text)` for every fence-aware node, in file order.
    Shares `_node_starts`, so a `[N]` inside a code fence is never a node."""
    lines = content.splitlines(keepends=True)
    starts, _in_fence = _node_starts(lines)
    out = []
    for k, (idx, nid) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        text = "".join(lines[idx:end])
        out.append((nid, _node_title(lines[idx])[1], text))
    return out


def _read_map(path: Path) -> "str | None":
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
    except OSError:
        return None


_WORD_RE = re.compile(r"[a-z0-9_]+")
# A node may declare synonyms the prose doesn't spell out, so a lexical search
# still finds it by meaning: `<!-- keywords: auth, login, identity -->` on any
# line of the node. These are weighted heavily in ranking (author intent).
_KEYWORDS_RE = re.compile(r"<!--\s*keywords?:\s*(.*?)\s*-->", re.IGNORECASE)
_RECALL_TOP = 12


def _stem(w: str) -> str:
    """Strip a single plural `s` (gate/gates, hook/hooks) — the reliable case.
    Deliberately NOT a full stemmer: aggressive suffix rules (`-ing`/`-ed`) merge
    unrelated words (gate/gating), and a wrong merge is worse than a missed one in
    a search an agent trusts. `ss` is preserved (class stays class)."""
    if len(w) >= 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _tokenize(text: str) -> "list[str]":
    return [_stem(w) for w in _WORD_RE.findall(text.lower())]


def _build_corpus(main: str, overflow: "str | None") -> "dict[int, dict]":
    """{node_id: {title, tokens, sources}} merging both tiers per id. A node's
    `<!-- keywords -->` synonyms are folded in at 3× weight (author intent)."""
    docs: "dict[int, dict]" = {}

    def add(content: str, src: str):
        for nid, title, text in _iter_map_nodes(content):
            kw = " ".join(_KEYWORDS_RE.findall(text))
            toks = _tokenize(text) + _tokenize(kw) * 3
            if nid in docs:
                docs[nid]["tokens"].extend(toks)
                docs[nid]["sources"].add(src)
                if not docs[nid]["title"]:
                    docs[nid]["title"] = title
            else:
                docs[nid] = {"title": title, "tokens": toks, "sources": {src}}

    add(main, "main")
    if overflow:
        add(overflow, "overflow")
    return docs


def _rank_nodes(docs: "dict[int, dict]", query_terms: "list[str]") -> "list[tuple[int, float]]":
    """BM25 relevance ranking of nodes against the (stemmed) query terms.

    OR by nature — a node matching any term scores — but a node matching MORE of
    the terms, and rarer ones, ranks higher, so multi-word queries still favour
    the node that has all of them without excluding partial matches (the failure
    mode of the old hard-AND: `policy storage` returned nothing). Pure stdlib,
    offline — no embeddings, no index to rebuild — chosen to keep the plugin
    portable; this is 'better than grep' within that contract, not a vector DB."""
    n = len(docs)
    if n == 0:
        return []
    df: "dict[str, int]" = {}
    tfs: "dict[int, Counter]" = {}
    total_len = 0
    for nid, d in docs.items():
        tf = Counter(d["tokens"])
        tfs[nid] = tf
        total_len += sum(tf.values())
        for t in tf:
            df[t] = df.get(t, 0) + 1
    avgdl = (total_len / n) or 1.0
    k1, b = 1.5, 0.75
    scored = []
    for nid, tf in tfs.items():
        dl = sum(tf.values()) or 1
        score = 0.0
        for q in query_terms:
            f = tf.get(q, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scored.append((nid, score))
    scored.sort(key=lambda p: (-p[1], p[0]))
    return scored


def cmd_recall(cmd_args) -> None:
    """`tasks recall <id|keyword...>` — fetch mind-map content across both tiers.

    - `recall 12` (all-digits): print node [12] from MIND_MAP.md AND from
      MIND_MAP_OVERFLOW.md (the fuller detail), each labeled; note when one tier
      lacks it.
    - `recall auth policy` (words): a RANKED relevance search (BM25 + plural
      stemming + node `<!-- keywords -->` synonyms) over both files — best match
      first — so a topic resolves to the node ids to `recall <N>` in full.
    """
    positional = [a for a in cmd_args if not a.startswith("-")]
    if not positional:
        print("'recall' requires a node id or keyword(s) "
              "(e.g. `tasks recall 12`, `tasks recall auth policy`).", file=sys.stderr)
        sys.exit(1)

    project_path = find_project_root()
    main = _read_map(project_path / "MIND_MAP.md")
    overflow = _read_map(project_path / "MIND_MAP_OVERFLOW.md")
    if main is None:
        print("Error: MIND_MAP.md not found — nothing to recall.", file=sys.stderr)
        sys.exit(1)

    # Fail loud (as the rest of the module does) when a map has an unbalanced
    # ``` code fence: node boundaries after it are unreliable, so retrieval may
    # miss or misattribute nodes. Warn, then answer best-effort.
    for _label, _content in (("MIND_MAP.md", main), ("MIND_MAP_OVERFLOW.md", overflow)):
        if _content and _node_starts(_content.splitlines(keepends=True))[1]:
            print(f"⚠ {_label} has an unbalanced ``` code fence — node boundaries "
                  "after it are unreliable; results may be incomplete. Run `tasks audit`.",
                  file=sys.stderr)

    # ── node-id mode ── (ASCII digits only: str.isdigit() also passes for '²'/'⁵'
    # etc., which int() then rejects — guard so a superscript falls to keyword
    # search instead of crashing).
    if len(positional) == 1 and positional[0].isascii() and positional[0].isdigit():
        nid = int(positional[0])
        main_list = _iter_map_nodes(main)
        main_nodes = {n: (t, x) for n, t, x in main_list}
        over_nodes = {n: (t, x) for n, t, x in _iter_map_nodes(overflow)} if overflow else {}
        dup = sum(1 for n, _, _ in main_list if n == nid) > 1
        if nid not in main_nodes and nid not in over_nodes:
            print(f"No node [{nid}] in MIND_MAP.md"
                  f"{' or MIND_MAP_OVERFLOW.md' if overflow else ''}. "
                  "Run `tasks recall <keyword>` or `tasks bootstrap` to see the index.")
            return
        if dup:
            print(f"⚠ node [{nid}] is defined more than once in MIND_MAP.md — "
                  "showing the last; run `tasks audit` to fix the duplicate.")
        if nid in main_nodes:
            print(f"=== [{nid}] from MIND_MAP.md ===")
            print(main_nodes[nid][1].rstrip())
        if nid in over_nodes:
            print(f"\n=== [{nid}] from MIND_MAP_OVERFLOW.md (fuller detail) ===")
            print(over_nodes[nid][1].rstrip())
        elif overflow is not None and nid in main_nodes:
            print(f"\n(no [{nid}] in overflow — MIND_MAP.md holds the full node)")
        return

    # ── keyword mode (ranked) ──
    query = " ".join(positional)
    qterms = _tokenize(query)
    docs = _build_corpus(main, overflow)
    ranked = _rank_nodes(docs, qterms) if qterms else []
    if not ranked:
        print(f"No node matched {query!r}. "
              "Try broader/fewer words, or `tasks bootstrap` for the full index.")
        return
    print(f"{len(ranked)} node(s) matched {query!r}, best first:")
    for nid, _score in ranked[:_RECALL_TOP]:
        src = docs[nid]["sources"]
        tag = ("main+overflow" if len(src) > 1
               else "overflow" if "overflow" in src else "main")
        print(f"  [{nid}] {docs[nid]['title']}  ({tag})")
    if len(ranked) > _RECALL_TOP:
        print(f"  … and {len(ranked) - _RECALL_TOP} more")
    print("Fetch one in full: tasks recall <N>")
