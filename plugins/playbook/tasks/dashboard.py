#!/usr/bin/env python3
"""`tasks dashboard` — what playbook runs on RIGHT NOW, read-only.

One screen: plugin version, verify command, the judge panel (seat + effort +
default judge + `panel_required_for`), the review knobs (soft/hard timeout,
judge budget), hooks health (the same checks `tasks doctor` runs), open and
parked tasks, the judgebench corpus + last exam per seat pair (dev-only harness,
absent on most installs — shown as absent, never invented), and per seat over
the last 14 days from the review-spend journal: runs, ok %, timeout %, median
duration.

Exactly THREE triggers, each printed with the exact command that acts on it:

  1. drift     — a seat whose timeout rate or median duration DOUBLED against
                 its previous 30 days (days 15–44 back);
  2. model-gap — a SEATED model that the installed codex / grok CLI no longer
                 lists (a dead pin), or a model id that is NEW since the
                 dashboard last recorded the provider catalog in the baseline
                 file `.agent/model-catalog.json` (task 054). "Available but not
                 seated" is an informational line, never a trigger — a selective
                 panel is a choice, not a defect;
  3. template  — the judgebench judge prompt template changed since the last
                 live exam.

CONTRACT: this module changes NO setting, and the `dashboard` CLI arm is
dispatched BEFORE the session garbage-collector every other command runs
(plan-panel codex#1), so `tasks dashboard` touches no task/session state
end-to-end — not just below the CLI boundary. Its ONE write is the model-catalog
baseline `.agent/model-catalog.json` (machine-local, gitignored like
`models.json`): the provider ids the listing returned, so the next run can say
what is NEW. It is written atomically, best-effort (a failure is a printed note,
never a crash), only when the listing ran (`--no-detect` never writes) and only
when the catalog changed (no mtime churn). A trigger is a printed command the
operator runs (or does not). It reads the same files the writers own
(`.agent/config.json`, `.agent/models.json`, the lane journal, `bench/`),
tolerates every malformed input (a bad line is skipped and counted, never
raised on), and needs no network except the optional provider listing behind
trigger 2 (`grok models` is login-aware; `--no-detect` skips it).

The per-seat health window is the last WINDOW_DAYS, but never reaches back
past the most recent panel change (the mtime of the `.agent/models.json` the
panel was resolved from — only `tasks models set/select` or a hand edit write
it), so the stats describe the seats configured NOW (task 054).

The review-spend record shape is the external contract documented in
`docs/enforcement-journal.md`; the tolerant parse here mirrors what the external
reader (playbook-lens, a separate repo) does — same filter (`hook == "review"`),
same "count and never impute" stance — without importing it (stdlib only).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat as _stat
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tasks.shared import find_project_root

# ── constants ────────────────────────────────────────────────────────────────
WINDOW_DAYS = 14          # the "now" window the per-seat stats cover
BASELINE_DAYS = 30        # the comparison window right before it (drift trigger)
MIN_RUNS_FOR_DRIFT = 3    # fewer runs in EITHER window → no verdict, no trigger
DRIFT_FACTOR = 2.0        # "doubled"
MODEL_GAP_CAP = 6         # ids listed per provider before "+N more"
_GAP_PROVIDERS = ("claude", "codex", "grok")     # providers the model-gap check reads
# Providers whose listing is the CLI's own CATALOG (codex models cache, `grok
# models`): a seated id missing from it is a dead pin. Claude has no list
# command — its "listing" is the model(s) configured in settings.json, so a
# seat absent from it is unknown, never dead (only `tasks models check` probes).
_CATALOG_PROVIDERS = frozenset({"codex", "grok"})
CATALOG_BASELINE_REL = ".agent/model-catalog.json"
_MAX_NUMERIC = 10 ** 15 - 1   # the producer's magnitude cap (pb_journal._cap_int)

# Effort vocabularies — mirrored from the adapters so a `provider:model:effort`
# spec splits the same way review.py resolves it. Imported lazily where the
# adapters are importable; these are the fallback.
_CODEX_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_GROK_EFFORTS = frozenset({"low", "medium", "high"})
_CLAUDE_EFFORT = "high"   # fixed — see review._CLAUDE_JUDGE_EFFORT
_KNOWN_STATUSES = frozenset({"ok", "fail", "timeout", "dnf"})   # docs/enforcement-journal.md


# ── small pure helpers ───────────────────────────────────────────────────────
def parse_ts(value) -> "datetime | None":
    """ISO-8601 → tz-aware datetime, or None. Python 3.10's fromisoformat rejects
    a trailing `Z`, so normalise it first; a naive result is assumed UTC."""
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def fmt_ms(ms: "int | None") -> str:
    """12345 → '12s'; 194587 → '3m15s'; None → 'n/a'."""
    if ms is None:
        return "n/a"
    secs = int(round(ms / 1000.0))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def pct(part: int, whole: int) -> str:
    """Integer percent or 'n/a' — never a division by zero, never a fake 0%."""
    if whole <= 0:
        return "n/a"
    return f"{int(round(100.0 * part / whole))}%"


def _one_line(value) -> str:
    """Collapse untrusted text (a seat, a status) to one printable line so a
    hostile journal value cannot forge a row or a heading."""
    s = "".join(" " if (c in "\r\n\t" or not c.isprintable()) else c for c in str(value))
    return " ".join(s.split())


# ── review-spend journal ─────────────────────────────────────────────────────
def journal_path(project_path: Path) -> "Path | None":
    """The lane journal the review runner writes (`.agent[/<user>]/journal/
    enforcement.jsonl`). None when the lane cannot be resolved — a dashboard
    must never mint a lane the enforcing resolvers refuse."""
    try:
        from tasks.core import resolve_agent_dir
        agent_dir = resolve_agent_dir(Path(project_path))
    except SystemExit:
        return None
    except Exception:
        return None
    return agent_dir / "journal" / "enforcement.jsonl"


_JOURNAL_READ_CAP = 64 * 1024 * 1024   # bytes — a journal grown past this is truncated at the FRONT, never OOM


def _read_regular_text(path: Path, cap: int = _JOURNAL_READ_CAP) -> "str | None":
    """Read `path` ONLY if it is a plain regular file — the same guard the
    journal writers use (plan-panel grok#3): a hostile swap of the journal path
    to a FIFO/symlink/device must not hang a bootstrap. None when not readable
    as such. See `_read_regular_tagged` for the status-carrying form."""
    text, _status = _read_regular_tagged(path, cap)
    return text


def _read_regular_tagged(path: Path, cap: int = _JOURNAL_READ_CAP) -> "tuple[str | None, str]":
    """(text, status) with status ∈ {ok, missing, unreadable, truncated}. One
    `O_NONBLOCK|O_NOFOLLOW` open (flags absent on Windows → getattr 0, plain
    read), `fstat` re-validation AFTER the open. A file larger than `cap` is read
    from its TAIL (newest records — impl-panel codex#4: the head is the oldest
    data, useless for a 14-day window), dropping the first partial line, and
    tagged `truncated` so the render can say so instead of presenting a partial
    total as the total."""
    flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return None, "unreadable"
        truncated = st.st_size > cap
        if truncated:
            os.lseek(fd, st.st_size - cap, os.SEEK_SET)
        chunks = []
        remaining = cap
        while remaining > 0:
            b = os.read(fd, min(1 << 20, remaining))
            if not b:
                break
            chunks.append(b)
            remaining -= len(b)
        raw = b"".join(chunks)
        if truncated:
            nl = raw.find(b"\n")
            raw = raw[nl + 1:] if nl >= 0 else b""
        return raw.decode("utf-8", "replace"), ("truncated" if truncated else "ok")
    except OSError:
        return None, "unreadable"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def load_review_records(path: "Path | None") -> "tuple[list[dict], int]":
    """(records, skipped) — see `load_review_journal` for the status-carrying form."""
    records, skipped, _status = load_review_journal(path)
    return records, skipped


def load_review_journal(path: "Path | None", cap: int = _JOURNAL_READ_CAP) -> "tuple[list[dict], int, str]":
    """Every `hook == "review"` record in the journal, normalised to
    {ts, seat, kind, task, status, duration_ms|None}. Returns (records, skipped,
    status) — status ∈ {ok, missing, unreadable, truncated, unresolved} so the
    render can tell "no spend" from "could not read" (impl-panel codex#4). A line
    that is not JSON, not an object, not a review record, or lacks a parseable
    `ts`/`seat`/`status` is skipped and COUNTED, never raised on. Numbers are
    never imputed: a missing/invalid duration stays None."""
    out: "list[dict]" = []
    skipped = 0
    if path is None:
        return out, 0, "unresolved"
    text, read_status = _read_regular_tagged(Path(path), cap)
    if text is None:
        return out, 0, read_status
    raw_lines = text.splitlines()
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):   # deeply nested but valid JSON is still a bad line (r3 codex#3)
            skipped += 1
            continue
        if not isinstance(obj, dict) or obj.get("hook") != "review":
            continue
        ts = parse_ts(obj.get("ts"))
        seat = obj.get("seat")
        status = obj.get("status")
        if (ts is None or not isinstance(seat, str) or not seat
                or not isinstance(status, str) or status not in _KNOWN_STATUSES):
            skipped += 1        # an out-of-contract status is malformed, not a run (impl-panel r2 codex-med#4)
            continue
        dur = obj.get("duration_ms")
        if not _is_int(dur) or dur < 0 or dur > _MAX_NUMERIC:
            dur = None
        out.append({
            "ts": ts,
            "seat": _one_line(seat),
            "kind": _one_line(obj.get("kind", "?")),
            "task": _one_line(obj.get("task", "-")),
            "status": _one_line(status),
            "duration_ms": dur,
        })
    return out, skipped, read_status


def other_lane_journals(project_path: Path, resolved: "Path | None") -> "list[str]":
    """Lanes OTHER than the resolved one that carry a review journal — surfaced
    by name, never aggregated (plan-panel opus#4 / codex#5 / grok#4: the writer
    is lane-resolved; a multi-user repo must not read as 'zero spend' silently).
    Mirrors playbook-lens's visible note."""
    out: "list[str]" = []
    try:
        from tasks.core import _agent_lanes
        for user, rel in _agent_lanes(Path(project_path)):
            j = Path(project_path) / rel / "journal" / "enforcement.jsonl"
            if resolved is not None and j.resolve() == Path(resolved).resolve():
                continue
            if j.is_file():
                out.append(user or "(root)")
    except Exception:
        pass
    return out


def _window(records: "list[dict]", start: datetime, end: datetime) -> "list[dict]":
    """Records with start < ts <= end."""
    return [r for r in records if start < r["ts"] <= end]


def seat_stats(records: "list[dict]") -> "dict[str, dict]":
    """Per seat: runs, ok, timeout, median_ms (None when no record carried a
    duration). No division here — the renderer formats percentages."""
    by: "dict[str, dict]" = {}
    for r in records:
        s = by.setdefault(r["seat"], {"runs": 0, "ok": 0, "timeout": 0, "durations": []})
        s["runs"] += 1
        if r["status"] == "ok":
            s["ok"] += 1
        elif r["status"] == "timeout":
            s["timeout"] += 1
        if r["duration_ms"] is not None:
            s["durations"].append(r["duration_ms"])
    for s in by.values():
        d = s.pop("durations")
        s["median_ms"] = int(statistics.median(d)) if d else None
    return by


def window_stats(records: "list[dict]", now: datetime,
                 days: int = WINDOW_DAYS) -> "dict[str, dict]":
    """`seat_stats` over (now - days, now]."""
    return seat_stats(_window(records, now - timedelta(days=days), now))


def panel_changed_at(project_path: Path) -> "datetime | None":
    """When the panel last changed: the mtime of the `.agent/models.json` the
    panel is resolved from (the same walk-up `load_judge_config` uses, so an
    inherited ancestor file counts). None on plugin defaults or any error."""
    try:
        from provider.sandbox import _find_project_models_override
        used = _find_project_models_override(Path(project_path))
        if used is None:
            return None
        return datetime.fromtimestamp(used.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None


def window_bounds(now: datetime, changed_at: "datetime | None") -> "tuple[datetime, str]":
    """(start, label) of the health window: the last WINDOW_DAYS, cut at the most
    recent panel change so the stats describe the seats configured NOW. A change
    stamped in the future (clock skew) is capped at `now`, never an inverted
    window."""
    start = now - timedelta(days=WINDOW_DAYS)
    if changed_at is not None and changed_at > start:
        start = min(changed_at, now)
        age = (now - start).days
        return start, (f"since panel change {start.strftime('%Y-%m-%d %H:%M')}Z, "
                       f"{age}d ago — shorter than {WINDOW_DAYS}d")
    return start, f"last {WINDOW_DAYS} days"


# ── the panel ────────────────────────────────────────────────────────────────
def _split_effort(provider: str, variant: "str | None") -> "tuple[str | None, str]":
    """`gpt-5.6-sol:high` → ('gpt-5.6-sol', 'high'); claude → fixed effort;
    a variant with no recognised suffix → (variant, '(provider default)')."""
    if provider == "claude":
        return variant, _CLAUDE_EFFORT
    if not variant:
        return None, "(provider default)"
    vocab = _CODEX_EFFORTS if provider == "codex" else _GROK_EFFORTS if provider == "grok" else frozenset()
    if ":" in variant:
        head, _, tail = variant.rpartition(":")
        if tail in vocab:
            return head, tail
    return variant, "(provider default)"


def seat_label(provider: str, variant: "str | None") -> str:
    """The journal's `seat` spelling for a panel spec — the SAME rule the review
    runner applies (`review._seat_with_effort`): codex/grok carry the effort
    inside the variant; claude appends its fixed effort."""
    base = f"{provider}:{variant}" if variant else provider
    return f"{base}:{_CLAUDE_EFFORT}" if provider == "claude" else base


def panel_seats(project_path: Path) -> "dict":
    """The live panel: {"panel": [spec…], "default_judge": spec, "seats": [
    {spec, provider, model, effort, label, error}], "required_for": [...],
    "source": path-or-note}. Resolution goes through the shipped judge-spec
    resolver so an alias (`opus`) maps to the model id the journal records."""
    try:
        from provider.sandbox import load_judge_config, resolve_judge_spec
        # models.json (judge selection), NOT .agent/config.json — named apart so
        # the config-doc drift instrument (test_config_doc_drift) does not read
        # these keys as undocumented config.json keys.
        judge_cfg = load_judge_config(Path(project_path))
    except Exception as e:  # advisory — a broken models.json is a row, not a crash
        return {"panel": [], "default_judge": "", "seats": [], "required_for": [],
                "source": f"unreadable ({_one_line(e)})"}
    # config-sourced strings are untrusted text for the RENDER too (r3 grok#3 /
    # codex-med#3): a `\n` inside a valid JSON spec must not split the six-line
    # block or forge a heading
    panel = [_one_line(x) for x in judge_cfg.get("panel") if isinstance(x, str)] if isinstance(judge_cfg.get("panel"), list) else []
    dj = _one_line(judge_cfg.get("default_judge")) if isinstance(judge_cfg.get("default_judge"), str) else ""
    seats = []
    for spec in panel:
        if not isinstance(spec, str):
            continue
        try:
            prov, variant = resolve_judge_spec(spec)
            model, effort = _split_effort(prov, variant)
            seats.append({"spec": spec, "provider": prov, "model": _one_line(model or ""),
                          "effort": _one_line(effort), "label": _one_line(seat_label(prov, variant)), "error": ""})
        except ValueError as e:
            seats.append({"spec": spec, "provider": "?", "model": "", "effort": "?",
                          "label": spec, "error": _one_line(e)})
    # the label must name the file whose bytes were used (impl-panel grok#1):
    # `load_judge_config` walks UP for `.agent/models.json`, so a nested project
    # without its own file inherits an ancestor's panel — say so.
    try:
        from provider.sandbox import _find_project_models_override
        used = _find_project_models_override(Path(project_path))
    except Exception:
        used = None
    if used is None:
        source = "plugin defaults (no .agent/models.json here or above)"
    elif used.resolve() == (Path(project_path) / ".agent" / "models.json").resolve():
        source = ".agent/models.json"
    else:
        source = f"{used} (an ANCESTOR project's models.json — not this project's)"
    return {"panel": panel, "default_judge": dj,
            "seats": seats, "required_for": [_one_line(r) for r in panel_required_for(project_path)],
            "source": _one_line(source)}


def panel_required_for(project_path: Path) -> "list[str]":
    """The EFFECTIVE close policy — which risk classes require a quorum panel —
    resolved through `core.resolve_panel_required` (the close's own reader), not
    the raw config value (r4 codex#1: a malformed scalar like `"assertive"` reads
    as NO requirement at close time, and the dashboard must say so). A raw value
    the resolver does not honour is flagged alongside."""
    try:
        from tasks.core import load_config, resolve_panel_required
        effective = [r for r in ("reversible", "assertive", "irreversible")
                     if resolve_panel_required(Path(project_path), r)]
        raw = load_config(Path(project_path)).get("panel_required_for")
    except Exception:
        return ["(unreadable)"]
    out = list(effective)
    if raw is not None and not effective:
        out.append(f"(raw {_one_line(json.dumps(raw))} is not honoured by the close — no panel required)")
    return out


def review_knobs(project_path: Path) -> "dict":
    """Resolved soft/hard timeouts + judge budget, via the SAME resolvers the
    review runner uses (env > config > default)."""
    from tasks.core import (resolve_judge_budget, resolve_review_soft_timeout,
                            resolve_review_timeout)
    p = Path(project_path)
    hard = resolve_review_timeout(p)
    soft = resolve_review_soft_timeout(p, hard)
    return {"soft": soft, "hard": hard, "budget_usd": resolve_judge_budget(p)}


# ── trigger 1: drift ─────────────────────────────────────────────────────────
def _models_set_command(panel: "dict", drop_label: "str | None" = None,
                        add_spec: "str | None" = None) -> str:
    """The exact `tasks models set` line that would apply a one-seat change —
    always paste-ready and non-interactive (`run_set` accepts `--default-judge`
    with or without `--panel`)."""
    specs = list(panel.get("panel") or [])
    dj = panel.get("default_judge") or ""
    note = ""
    if drop_label is not None:
        keep = [s["spec"] for s in panel.get("seats", []) if s["label"] != drop_label]
        if not keep:
            return ""            # nothing executable to print — the caller suppresses the alarm (r4 codex#4)
        dj_label = _label_of_spec(dj) or dj    # alias vs canonical spec compare by LABEL (r4 codex#2 / grok#1)
        if dj_label == drop_label:
            # the dropped seat IS the default judge: `models set` needs a new one,
            # so propose the first remaining seat and say so (plan-panel grok#2:
            # never print the interactive `models select` — it blocks on input()).
            dj = keep[0]
            note = "   # the dropped seat was the default judge — pick the default you want"
        specs = keep
    if add_spec is not None:
        specs = specs + [add_spec]
    # shlex-quoted (impl-panel codex-med#5): a detected id or configured variant is
    # untrusted text and must not become shell substitution when pasted.
    dj_part = f" --default-judge {shlex.quote(dj)}" if dj else ""
    return f"tasks models set --panel {shlex.quote(','.join(specs))}{dj_part}{note}"


def _label_of_spec(spec: str) -> str:
    """The journal seat label a panel spec resolves to ("" when unresolvable)."""
    try:
        from provider.sandbox import resolve_judge_spec
        prov, variant = resolve_judge_spec(spec)
        return seat_label(prov, variant)
    except Exception:
        return ""


def drift_triggers(records: "list[dict]", now: datetime, panel: "dict",
                   start: "datetime | None" = None) -> "list[dict]":
    """Seats whose timeout rate or median duration doubled: the health window
    (`start`, now] — default the last WINDOW_DAYS, or the bounded window from
    `window_bounds` — vs the BASELINE_DAYS right before ITS start. Both windows
    need MIN_RUNS_FOR_DRIFT runs, else there is no verdict (stated, not silently
    skipped). A baseline timeout rate of 0 with current timeouts counts as
    doubled (0 → >0)."""
    start = start if start is not None else now - timedelta(days=WINDOW_DAYS)
    cur = seat_stats(_window(records, start, now))
    base = seat_stats(_window(records, start - timedelta(days=BASELINE_DAYS), start))
    out = []
    for seat in sorted(cur):
        c, b = cur[seat], base.get(seat)
        if b is None or c["runs"] < MIN_RUNS_FOR_DRIFT or b["runs"] < MIN_RUNS_FOR_DRIFT:
            continue
        c_rate, b_rate = c["timeout"] / c["runs"], b["timeout"] / b["runs"]
        reasons = []
        if c["timeout"] > 0 and c_rate >= DRIFT_FACTOR * b_rate:
            reasons.append(f"timeout rate {pct(c['timeout'], c['runs'])} vs {pct(b['timeout'], b['runs'])} in the prior 30d")
        if (c["median_ms"] is not None and b["median_ms"] not in (None, 0)
                and c["median_ms"] >= DRIFT_FACTOR * b["median_ms"]):
            reasons.append(f"median {fmt_ms(c['median_ms'])} vs {fmt_ms(b['median_ms'])} in the prior 30d")
        if not reasons:
            continue
        if not any(s["label"] == seat for s in panel.get("seats", [])):
            # a seat that is no longer configured has no actionable command (nothing
            # to drop) — it stays visible as a "(not in panel)" row, but is not an
            # alarm (impl-panel codex#3: every alarm carries a real command).
            continue
        cmd = _models_set_command(panel, drop_label=seat)
        if not cmd:
            continue             # the panel's only seat: no executable one-line remedy exists (r4 codex#4)
        out.append({"kind": "drift", "seat": seat, "detail": "; ".join(reasons), "command": cmd})
    return out


# ── trigger 2: model gap ─────────────────────────────────────────────────────
def _bare_model(model: str) -> str:
    """`claude-opus-4-8[1m]` and `claude-opus-4-8` are one id for the gap check."""
    return re.sub(r"\[.*?\]\s*$", "", model or "").strip()


def _listing(detect_report: "dict | None") -> "dict[str, list[tuple[str, str, list]]]":
    """Per in-scope INSTALLED provider, its listed models as (raw id, bare id,
    efforts), deduplicated by bare id in listing order. A provider that is not
    installed or listed nothing is absent — unknown, not empty."""
    out: "dict[str, list[tuple[str, str, list]]]" = {}
    if not isinstance(detect_report, dict):
        return out
    for prov in detect_report.get("providers", []) or []:
        if not isinstance(prov, dict):
            continue
        name = prov.get("name")
        if name not in _GAP_PROVIDERS or not prov.get("installed"):
            continue
        seen: "set[str]" = set()
        items = []
        for m in prov.get("models", []) or []:
            mid = m.get("id") if isinstance(m, dict) else None
            if not isinstance(mid, str) or not mid:
                continue
            bare = _bare_model(mid)
            if not bare or bare in seen:
                continue
            seen.add(bare)
            eff = m.get("efforts") if isinstance(m.get("efforts"), list) else []
            items.append((mid, bare, [e for e in eff if isinstance(e, str)]))
        if items:
            out[name] = items
    return out


def catalog_from_report(detect_report: "dict | None") -> "dict[str, list[str]]":
    """The catalog the baseline records: provider → sorted bare ids (installed,
    in-scope providers with a non-empty listing only)."""
    return {prov: sorted(b for _raw, b, _e in items) for prov, items in _listing(detect_report).items()}


def _seated(panel: "dict") -> "set[tuple[str, str]]":
    return {(s["provider"], _bare_model(s["model"])) for s in panel.get("seats", []) if s.get("model")}


def _add_spec(prov: str, raw_id: str, efforts: "list[str]") -> str:
    """`prov:id[:effort]` with the effort from the model's OWN advertised
    vocabulary (impl-panel sonnet#2 / codex#3): "medium" when offered, else its
    first, else no suffix — never a hardcoded suffix `models set` would reject."""
    spec = f"{prov}:{raw_id}"
    if efforts:
        spec += ":" + ("medium" if "medium" in efforts else efforts[0])
    return spec


def model_gap_info(detect_report: "dict | None", panel: "dict") -> "list[str]":
    """Informational, never a trigger: per provider, the listed ids no seat uses
    (bare-id compare so an alias-resolved seat is not "unseated"). One
    `provider: id, id (+N more)` string per provider with any."""
    seated = _seated(panel)
    out = []
    for prov, items in _listing(detect_report).items():
        unseated = [raw for raw, bare, _e in items if (prov, bare) not in seated]
        if not unseated:
            continue
        shown = unseated[:MODEL_GAP_CAP]
        more = len(unseated) - len(shown)
        out.append(f"{prov}: {', '.join(shown)}" + (f" (+{more} more — tasks models detect)" if more else ""))
    return out


def model_gap_triggers(detect_report: "dict | None", panel: "dict",
                       baseline: "dict | None" = None) -> "list[dict]":
    """Two reasons fire, both kind `model-gap` (task 054):

    * dead pin — a seat whose provider is a CATALOG provider (codex/grok) that
      is installed and listed models, but not the seated one. `seat` is the
      seat's journal label; the command drops it (or, for a sole seat, is
      `tasks models check`, the only executable remedy). Listed ≠ probed — the
      detail says to confirm with `tasks models check` first. Claude's listing
      is what settings.json configures, not a catalog → never a dead pin; an
      uninstalled provider or an empty listing is unknown, not dead.
    * new id — a listed bare id absent from `baseline["providers"][prov]`
      (the catalog the dashboard last recorded) and not already seated. One
      trigger per provider, ids capped at MODEL_GAP_CAP, the add command shown
      for the first. No baseline (first run, or unreadable) → no verdict.

    "Available but not seated" alone never fires — see `model_gap_info`."""
    listing = _listing(detect_report)
    if not listing:
        return []
    seated = _seated(panel)
    out: "list[dict]" = []
    for s in panel.get("seats", []):
        prov = s.get("provider")
        if prov not in _CATALOG_PROVIDERS or not s.get("model") or s.get("error"):
            continue
        items = listing.get(prov)
        if not items:
            continue                         # not installed / listed nothing → unknown, no verdict
        bare = _bare_model(s["model"])
        if any(b == bare for _raw, b, _e in items):
            continue
        cmd = _models_set_command(panel, drop_label=s["label"]) or "tasks models check"
        out.append({"kind": "model-gap", "seat": s["label"],
                    "detail": f"dead pin: {bare} is no longer listed by {prov} (listed ≠ probed — "
                              f"`tasks models check` confirms before you drop it)",
                    "command": cmd})
    base_prov = baseline.get("providers") if isinstance(baseline, dict) else None
    if not isinstance(base_prov, dict):
        return out
    since = _date(parse_ts(baseline.get("recorded_at")))
    for prov, items in listing.items():
        known_raw = base_prov.get(prov)
        known = {k for k in known_raw if isinstance(k, str)} if isinstance(known_raw, list) else set()
        new = [(raw, bare, eff) for raw, bare, eff in items if bare not in known and (prov, bare) not in seated]
        if not new:
            continue
        shown = new[:MODEL_GAP_CAP]
        more = len(new) - len(shown)
        first_raw, _b, eff = shown[0]
        out.append({"kind": "model-gap", "seat": prov,
                    "detail": f"new since {since}: {', '.join(r for r, _b2, _e2 in shown)}"
                              + (f" (+{more} more — tasks models detect)" if more else "")
                              + f" (listed by {prov}, not seated; listed ≠ probed — `tasks models check` confirms entitlement)",
                    "command": _models_set_command(panel, add_spec=_add_spec(prov, first_raw, eff))
                               + (f"   # shown for {first_raw}; substitute any id above" if len(new) > 1 else "")})
    return out


# ── the catalog baseline (the module's one write) ────────────────────────────
def catalog_baseline_path(project_path: Path) -> Path:
    return Path(project_path) / ".agent" / "model-catalog.json"


def load_catalog_baseline(path: Path) -> "dict | None":
    """The recorded catalog, or None when absent/unreadable/malformed (= no
    baseline, no new-id verdict — never a crash)."""
    try:
        data = _load_json_bounded(Path(path))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        return None
    return data


def record_catalog_baseline(project_path: Path, detect_report: "dict | None", now: datetime) -> str:
    """Write the current catalog as the new baseline — atomically, best-effort,
    and only when it differs from the recorded one. Returns the one-line note the
    render prints. Nothing is written without a listing (`--no-detect`, or a
    failed listing)."""
    rel = CATALOG_BASELINE_REL
    if not detect_report:
        return f"catalog baseline {rel} not recorded (no provider listing this run)"
    path = catalog_baseline_path(project_path)
    catalog = catalog_from_report(detect_report)
    prev = load_catalog_baseline(path)
    if prev is not None and prev.get("providers") == catalog:
        return f"catalog baseline {rel} unchanged since {_date(parse_ts(prev.get('recorded_at')))}"
    payload = {
        "recorded_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providers": catalog,
        "_doc": "Model ids the installed codex/grok/claude CLIs listed when `tasks dashboard` "
                "last ran (bare ids). The dashboard's model-gap trigger fires on ids NEW since "
                "this record; machine-local like models.json — do not commit.",
    }
    try:
        from tasks.atomic import atomic_write
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except Exception as e:
        return f"catalog baseline {rel} write failed ({_one_line(e)}) — new-id verdicts keep using the previous record"
    return (f"catalog baseline {rel} recorded now (first run — nothing is new yet)" if prev is None
            else f"catalog baseline {rel} updated")


# ── judgebench (dev-only harness) ────────────────────────────────────────────
def find_bench_dir(project_path: Path) -> "Path | None":
    """`bench/` with a frozen corpus, in the project root or a `code_roots`
    nested checkout. None on ordinary installs — the harness never ships."""
    roots = [Path(project_path)]
    try:
        from tasks.core import _code_roots, load_config
        roots += [Path(project_path) / r for r in _code_roots(load_config(Path(project_path)))]
    except Exception:
        pass
    for r in roots:
        cand = r / "bench"
        if (cand / "corpus" / "corpus.json").is_file():
            return cand
    return None


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_WIN_RE = re.compile(r"\b(?:wins?|does not win|loses?)\b")
_TABLE_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|(.*)\|\s*$")


def _decision_section(report_text: str) -> str:
    """The `## §25 decision` section's body (to the next `## `), else the
    whole report — the verdict sentence lives there."""
    lines = report_text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and "decision" in ln.lower():
            body = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("## "):
                    break
                body.append(nxt)
            return "\n".join(body)
    return report_text


def parse_report_verdict(report_text: str) -> "dict":
    """From a judgebench report.md: {"verdict": <the bold §25 verdict sentence>,
    "weighted": {label: int}}. Bold spans are paired sequentially (`**…**`), so a
    span can never straddle two bold phrases; the verdict is the LAST span in
    the decision section that speaks of winning/losing without being the rule
    itself ("wins only if") — the conclusion follows any restatement. Missing
    pieces stay empty — never invented."""
    # weighted table: the FIRST contiguous pipe-table whose header has (case-
    # insensitive) `candidate` + `weighted` columns; collection stops at the first
    # non-table line so a later numeric table cannot pollute it (r4 opus#1)
    weighted: "dict[str, int]" = {}
    header_idx = None
    for line in report_text.splitlines():
        m = _TABLE_ROW_RE.match(line)
        if not m:
            if header_idx is not None:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header_idx is None:
            low = [c.lower() for c in cells]
            if "weighted" in low and "candidate" in low:
                header_idx = low.index("weighted")
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if header_idx < len(cells) and cells[header_idx].isdigit():
            weighted[cells[0]] = int(cells[header_idx])
    # verdict: among bold win/lose spans that are not the rule ("only if"), prefer
    # the LAST one that names a candidate label (r4 opus#2: a bolded trailing
    # caveat without a label cannot displace the conclusion); else the last span
    spans = []
    for m in _BOLD_RE.finditer(_decision_section(report_text)):
        text = " ".join(m.group(1).split())
        if _WIN_RE.search(text) and "only if" not in text:
            spans.append(text)
    named = [t for t in spans if any(lab and lab in t for lab in weighted)]
    verdict = (named or spans or [""])[-1]
    return {"verdict": verdict, "weighted": weighted}


def judgebench_summary(bench_dir: "Path | None") -> "dict | None":
    """{"corpus_version", "cases", "exams": [newest live run per candidate pair:
    {run_id, date, labels, specs, template_sha, template_version, verdict,
    weighted}], "template_sha_now", "template_path"}. Only `mode == "live"` runs
    are exams — a fake/smoke run is never a verdict."""
    if bench_dir is None:
        return None
    out: "dict" = {"corpus_version": None, "cases": 0, "exams": [],
                   "template_sha_now": "", "template_path": "", "bench_dir": Path(bench_dir).as_posix()}
    corpus = _load_json_bounded(bench_dir / "corpus" / "corpus.json")
    if isinstance(corpus, dict):
        v = corpus.get("version")
        out["corpus_version"] = v if isinstance(v, (int, str)) and not isinstance(v, bool) else None
        cs = corpus.get("cases")
        out["cases"] = len(cs) if isinstance(cs, list) else 0      # `"cases": 7` is not a case list (r4 codex#5)
    tpl = bench_dir / "lib" / "templates" / "judge_prompt.md"
    if tpl.is_file():
        try:
            out["template_sha_now"] = hashlib.sha256(tpl.read_bytes()).hexdigest()
            out["template_path"] = str(tpl.relative_to(bench_dir.parent)) if tpl.is_relative_to(bench_dir.parent) else str(tpl)
        except OSError:
            pass
    runs_dir = bench_dir / "runs"
    exams: "dict[tuple, dict]" = {}
    if runs_dir.is_dir():
        for run in sorted(runs_dir.iterdir()):
            mf = run / "manifest.json"
            if not mf.is_file():
                continue
            m = _load_json_bounded(mf)
            if not isinstance(m, dict) or m.get("mode") != "live":
                continue
            raw_c = m.get("candidates")
            # exactly TWO candidate dicts in the raw list (r2 codex#4, r3 grok#2: counting
            # after dropping junk let a 3-entry list pass) — an exam compares ONE pair
            if not (isinstance(raw_c, list) and len(raw_c) == 2 and all(isinstance(c, dict) for c in raw_c)):
                continue
            # (label, spec) pairs in MANIFEST order — never re-sorted apart (impl-panel
            # grok#3): the re-exam command needs `label=spec` for every candidate.
            pairs = [(_one_line(c.get("label", "?")), _one_line(c.get("spec", "?"))) for c in raw_c]
            labels = tuple(sorted(lab for lab, _sp in pairs))
            if len(set(labels)) != 2:
                continue
            # keyed by the canonical SPECS (impl-panel codex#1): labels are mutable
            # display names; two exams of the same seats under renamed labels are one pair
            key = tuple(sorted(sp for _lab, sp in pairs))
            created = parse_ts(m.get("created_at"))
            tpl_info = m.get("template") if isinstance(m.get("template"), dict) else {}
            rep = run / "report.md"
            verdict = parse_report_verdict(_read_regular_text(rep, cap=8 * 1024 * 1024) or "") if rep.is_file() else {"verdict": "", "weighted": {}}
            if not verdict["verdict"] and not verdict["weighted"]:
                continue                    # a manifest is written BEFORE the first invocation: without a
                                            # report it is an interrupted run, not an exam (r3 codex#2)
            srcs = m.get("source_repos") if isinstance(m.get("source_repos"), dict) else {}
            tmo = m.get("timeouts") if isinstance(m.get("timeouts"), dict) else {}
            entry = {"run_id": _one_line(m.get("run_id") or run.name), "date": created,
                     "labels": list(labels), "candidates": pairs,
                     "specs": [sp for _lab, sp in pairs],
                     "template_sha": str(tpl_info.get("sha256") or ""),
                     "template_version": _one_line(tpl_info.get("version") or ""),
                     "verdict": verdict["verdict"] or "(no §25 verdict sentence in report.md)",
                     "weighted": verdict["weighted"],
                     # the run parameters an EXACT re-run needs (r3 codex#1, r4 codex#3: the case set too)
                     "run_params": {
                         "cases": [_one_line(c) for c in (m.get("corpus") or {}).get("cases", [])
                                   if isinstance(c, str)] if isinstance(m.get("corpus"), dict) and isinstance((m.get("corpus") or {}).get("cases"), list) else [],
                         "spec_mode": _one_line(m["spec_mode"]) if isinstance(m.get("spec_mode"), str) else "",
                         "concurrency": m["concurrency"] if _is_int(m.get("concurrency")) else None,
                         "soft_timeout": tmo["soft_secs"] if _is_int(tmo.get("soft_secs")) else None,
                         "timeout": tmo["hard_secs"] if _is_int(tmo.get("hard_secs")) else None,
                         "source_repos": {_one_line(k): _one_line(v.get("path"))
                                          for k, v in srcs.items() if isinstance(v, dict) and isinstance(v.get("path"), str)},
                     }}
            prev = exams.get(key)
            if prev is None or _exam_date_key(entry) >= _exam_date_key(prev):
                exams[key] = entry
    for e in exams.values():
        e["source"] = "bench/runs manifest"
    out["exams"] = sorted(exams.values(), key=_exam_date_key, reverse=True)
    return out


def _exam_date_key(e: dict) -> datetime:
    return e.get("date") or datetime.min.replace(tzinfo=timezone.utc)


def merge_record_exams(bench: "dict | None", records: "list[dict]") -> "dict | None":
    """Manifests win; a durable record fills in only a seat pair that has no
    manifest exam. With no bench dir at all, records alone form the summary
    (corpus version unknown, no template sha → the template trigger cannot fire
    and says so)."""
    if not records:
        return bench
    base = dict(bench) if bench else {"corpus_version": None, "cases": 0, "exams": [],
                                       "template_sha_now": "", "template_path": ""}
    have = {tuple(e["labels"]) for e in base["exams"]}
    # per label-pair keep the NEWEST dated record, not the first file found
    # (impl-panel sonnet#1 / codex#1 / grok#2); then one global newest-first sort
    newest: "dict[tuple, dict]" = {}
    for r in records:
        k = tuple(r["labels"])
        if k in have:
            continue
        if k not in newest or _exam_date_key(r) >= _exam_date_key(newest[k]):
            newest[k] = r
    base["exams"] = sorted(list(base["exams"]) + list(newest.values()), key=_exam_date_key, reverse=True)
    return base


def _load_json_bounded(path: Path, cap: int = 4 * 1024 * 1024):
    """JSON from a regular file, size-capped, never raising: None on any
    problem (missing, non-regular, over cap, malformed, too deeply nested)."""
    text, status = _read_regular_tagged(path, cap)
    if text is None or status == "truncated":
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


_RECORD_HEADER_RE = re.compile(r"^# judgebench report — run `([^`]+)`\s*$", re.M)
_RECORD_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def record_exams(project_path: Path) -> "list[dict]":
    """Exams recovered from DURABLE task records: any `*.md` under the lane's
    task dirs that embeds a judgebench report (`# judgebench report — run ...`).
    `bench/runs/` is gitignored (plan-panel codex#4 / codex-med#2), so on a
    fresh clone only these copies survive; they carry the table + §25 verdict but
    no manifest, so `template_sha` is empty and the date is the first ISO date
    in the report's run-facts text (or unknown). Newest-first is by run id
    order of appearance — records have no authoritative clock."""
    out: "list[dict]" = []
    try:
        from tasks.core import _iter_task_dirs
        dirs = [tf.parent for _n, _s, tf in _iter_task_dirs(Path(project_path))]
    except Exception:
        return out
    for d in dirs:
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            continue
        for f in files:
            # cheap sentinel probe first (r4 opus#3): most task markdown is not a report
            head = _read_regular_text(f, cap=64 * 1024)
            if not head or "judgebench report" not in head:
                continue
            text = _read_regular_text(f, cap=8 * 1024 * 1024) or ""
            for m in _RECORD_HEADER_RE.finditer(text):
                seg = text[m.end():]
                nxt = _RECORD_HEADER_RE.search(seg)
                seg = seg[:nxt.start()] if nxt else seg
                v = parse_report_verdict(seg)
                labels = sorted(v["weighted"])
                if len(labels) != 2:
                    continue                # exactly one pair, as for manifests
                dm = None
                for sec_m in re.finditer(r"^## Run facts.*?$", seg, re.M):
                    tail = seg[sec_m.end():sec_m.end() + 600]
                    dm = _RECORD_DATE_RE.search(tail)
                    break
                out.append({"run_id": m.group(1), "date": parse_ts(dm.group(1) + "T00:00:00Z") if dm else None,
                            "labels": labels, "candidates": [], "specs": [], "template_sha": "", "template_version": "",
                            "verdict": v["verdict"] or "(no verdict sentence in the record)",
                            "weighted": v["weighted"],
                            "source": f"record {d.name}/{f.name}"})
    return out


def _exams_with_fallback(project_path: Path) -> "dict | None":
    """Manifests, then durable task-record copies filling every seat pair that
    has no manifest exam (impl-panel r2 codex#2 / grok#2: one surviving manifest
    must not hide the other pairs). The record scan runs ONLY when a bench dir
    exists at all (r2 opus#2 / codex#5): an ordinary install has no judgebench
    and must pay nothing for it on every bootstrap; the fresh-clone case the
    fallback exists for (committed corpus, gitignored runs) still has `bench/`."""
    bench_dir = find_bench_dir(project_path)
    if bench_dir is None:
        return None
    bench = merge_record_exams(judgebench_summary(bench_dir), record_exams(project_path))
    if bench is not None:
        # the printed re-exam command uses a project-relative path when the bench
        # lives inside the project (e.g. `playbook-plugin/bench/judgebench.py`)
        try:
            bench["bench_dir_display"] = bench_dir.relative_to(Path(project_path)).as_posix()
        except ValueError:
            bench["bench_dir_display"] = Path(bench_dir).as_posix()
    return bench


# ── trigger 3: template ──────────────────────────────────────────────────────
def template_triggers(bench: "dict | None") -> "list[dict]":
    """The judgebench EXAM template (`bench/lib/templates/judge_prompt.md` — the
    instrument the exam ran with, NOT the live review.py panel prompt; plan-panel
    opus#3) differs from the sha the newest manifest-backed exam recorded → one
    trigger naming the exact re-exam command."""
    if not bench or not bench.get("exams") or not bench.get("template_sha_now"):
        return []
    newest = next((e for e in bench["exams"] if e.get("template_sha")), None)
    if newest is None or newest["template_sha"] == bench["template_sha_now"]:
        return []
    # `label=spec` per candidate, in manifest order — bench presets cover only the
    # sol-*/grok-* labels; any other label needs its spec (impl-panel codex#2 /
    # codex-med#1 / grok#3)
    pairs = newest.get("candidates") or [(lab, lab) for lab in newest["labels"]]
    cands = ",".join(f"{lab}={sp}" if sp and sp != lab else lab for lab, sp in pairs)
    # every interpolated token is manifest-controlled text → shlex-quoted (impl-panel
    # r2 codex-med#1 / grok#3), and the bench path is the one actually found (a
    # `code_roots` nested bench is not `bench/` at the project root — codex#3)
    bench_root = (bench.get("bench_dir_display") or bench.get("bench_dir") or "bench").rstrip("/")
    bench_py = bench_root + "/judgebench.py"
    new_id = _retest_run_id(newest["run_id"], Path(bench.get("bench_dir") or bench_root) / "runs")
    rp = newest.get("run_params") or {}
    cases = ",".join(rp.get("cases") or []) or "all"
    extra = ""
    if rp.get("spec_mode"):
        extra += f" --spec-mode {shlex.quote(rp['spec_mode'])}"
    if rp.get("concurrency") is not None:
        extra += f" --concurrency {rp['concurrency']}"
    if rp.get("soft_timeout") is not None:
        extra += f" --soft-timeout {rp['soft_timeout']}"
    if rp.get("timeout") is not None:
        extra += f" --timeout {rp['timeout']}"
    for name, path in sorted((rp.get("source_repos") or {}).items()):
        extra += f" --source-repo {shlex.quote(f'{name}={path}')}"
    return [{"kind": "template", "seat": newest["run_id"],
             "detail": (f"judgebench exam template is {bench['template_sha_now'][:12]} now, "
                        f"was {newest['template_sha'][:12]} at exam {newest['run_id']} "
                        f"({_date(newest['date'])}) — its verdict no longer describes the current exam prompt "
                        "(this tracks the bench instrument, not the live review.py panel prompt)"),
             "command": (f"python3 {shlex.quote(bench_py)} run --cases {shlex.quote(cases)} --candidates {shlex.quote(cands)} "
                         f"--run-id {shlex.quote(new_id)}{extra} --live")}]


_RUN_ID_MAX = 64          # bench/judgebench.py refuses longer ids


def _retest_run_id(base: str, runs_dir: Path) -> str:
    """`<base>-retest`, shortened to the harness's 64-char cap and made
    collision-free against existing run dirs (`-retest2`, `-retest3`, …) — a
    printed "exact" command must not be refused on paste (r3 codex-med#4)."""
    base = re.sub(r"[^A-Za-z0-9._-]", "-", base) or "exam"
    n = 1
    while True:
        suffix = "-retest" if n == 1 else f"-retest{n}"
        rid = base[: _RUN_ID_MAX - len(suffix)].rstrip("-.") + suffix
        try:
            exists = (runs_dir / rid).exists()
        except OSError:
            exists = False
        if not exists:
            return rid
        n += 1


def _date(dt: "datetime | None") -> str:
    return dt.strftime("%Y-%m-%d") if dt else "unknown date"


# ── hooks health (doctor's hook checks, summarised) ──────────────────────────
def hooks_health(project_path: Path) -> "dict":
    """{"ok": bool, "warnings": [str], "copy": str, "version": str, "covers": str}
    — the hook checks `tasks doctor` runs, re-derived from the same pure pieces
    (plan-panel codex#2): (1) hooks.json command shape/quoting across install
    copies (`hooks_check_report`); (2) the four enforcing hook scripts present
    and executable in the authoritative copy's `scripts/`; (3) grok's global
    enforcement file under doctor's own rule (stale paths always warn; a missing
    file only when the project is grok-bootstrapped via AGENTS.md); (4) doctor
    §4b stale `~/.claude/settings.json` hook paths; (5) doctor §8 gate-text
    truncation. Hook scripts are looked up in DOCTOR'S dir order (project
    `scripts/`, `.claude/hooks/`, `src/hooks/`, `$CLAUDE_PLUGIN_ROOT/scripts`,
    the running code's own `scripts/`), first existing copy wins — so the two
    commands report on the same file (r3 sonnet#1). NOT covered: doctor's
    non-hook checks (encoding, resolver parity, version). Advisory: never
    raises."""
    warnings: "list[str]" = []
    copy = version = ""
    covers = "manifest command shape · hook scripts present+executable · gate-text truncation · stale ~/.claude/settings.json hook paths · grok enforcement file"
    try:
        from tasks.hooks_check import (_code_version, _copy_version,
                                       authoritative_hooks_path, hooks_check_report)
        auth = authoritative_hooks_path()
        if auth is not None:
            copy = str(auth.parent.parent)
            version = _copy_version(auth) or _code_version()
        else:
            warnings.append("no authoritative hooks.json resolved (CLAUDE_PLUGIN_ROOT unset and no sibling hooks/)")
        # doctor §4's search order, first existing copy per hook — the SAME file
        # doctor would report on (r3 sonnet#1)
        hook_dirs = _doctor_hook_dirs(Path(project_path))
        for name in ("state-echo-hook", "task-gate-hook", "command-guard-hook", "stop-hook"):
            found = next((d / name for d in hook_dirs if (d / name).exists()), None)
            if found is None:
                warnings.append(f"hooks: {name} — missing (searched: {', '.join(str(d) for d in hook_dirs) or 'no hook dirs'})")
            elif not os.access(found, os.X_OK):
                warnings.append(f"hooks: {name} — found at {found.parent} but not executable")
        # doctor §8: the FIRST found state-echo hook must truncate gate text
        echo = next((d / "state-echo-hook" for d in hook_dirs if (d / "state-echo-hook").exists()), None)
        if echo is not None:
            body = _read_regular_text(echo, cap=1 << 20) or ""
            if "cut -c" not in body and "GATE_TEXT_STORE" not in body:
                warnings.append(f"hooks: gate text truncation — {echo} has no truncation (gate text may grow unbounded)")
        # doctor §4b: stale hook entries in ~/.claude/settings.json
        for stale in _stale_settings_hook_paths():
            warnings.append(f"hooks: stale ~/.claude/settings.json entry — {stale}")
        for label, detail in hooks_check_report(project_path):
            warnings.append(f"{label} — {detail}" if detail else label)
        # grok's always-trusted enforcement file — doctor's exact rule (diagnostics
        # 1g): a stale/broken script path warns whenever the file exists; a MISSING
        # file warns only when the project is grok-bootstrapped (AGENTS.md). W5
        # correspondence check caught the first draft gating everything on AGENTS.md
        # while doctor printed six stale-path warnings.
        from tasks.hooks_check import grok_enforcement_issues, grok_enforcement_report
        issues = grok_enforcement_issues()
        if issues:
            missing_only = all(i.startswith("missing ") for i in issues)
            if not missing_only or (Path(project_path) / "AGENTS.md").is_file():
                for label, detail in grok_enforcement_report():
                    warnings.append(f"{label} — {detail}" if detail else label)
    except Exception as e:  # advisory — a dashboard must never crash on it
        warnings.append(f"hooks check skipped ({_one_line(e)})")
    return {"ok": not warnings, "warnings": warnings, "copy": copy, "version": version, "covers": covers}


def _doctor_hook_dirs(project_path: Path, env: "dict | None" = None) -> "list[Path]":
    """Doctor §4's `hooks_dirs`, in its order: project `scripts/`, `.claude/hooks/`,
    `src/hooks/`, then `$CLAUDE_PLUGIN_ROOT/scripts` and the running code's own
    `scripts/` (doctor's home-glob last resort is deliberately not reproduced —
    it only applies when neither of those resolves, and then a dashboard should
    say "missing" rather than guess at a cache)."""
    env = os.environ if env is None else env
    dirs = [project_path / "scripts", project_path / ".claude" / "hooks", project_path / "src" / "hooks"]
    root = env.get("CLAUDE_PLUGIN_ROOT")
    if root and (Path(root) / "scripts").is_dir():
        dirs.append(Path(root) / "scripts")
    own = Path(__file__).resolve().parent.parent / "scripts"
    if own.is_dir():
        dirs.append(own)
    return [d for d in dirs if d.is_dir()]


def _stale_settings_hook_paths(settings_path: "Path | None" = None) -> "list[str]":
    """Doctor's §4b rule verbatim: every hook `command` token in
    `~/.claude/settings.json` that looks like a script path (suffix .sh or none,
    >2 parts) and does not exist. [] when the file is absent/malformed."""
    p = settings_path or (Path.home() / ".claude" / "settings.json")
    text = _read_regular_text(p, cap=4 * 1024 * 1024)
    if text is None:
        return []
    try:
        settings = json.loads(text)
    except ValueError:
        return []

    def _cmds(node):
        if isinstance(node, dict):
            c = node.get("command")
            if isinstance(c, str):
                yield c
            for v in node.values():
                yield from _cmds(v)
        elif isinstance(node, list):
            for item in node:
                yield from _cmds(item)

    out: "list[str]" = []
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    for cmd in _cmds(hooks):
        for token in cmd.split():
            tp = Path(token)
            if tp.suffix in (".sh", "") and len(tp.parts) > 2 and not tp.exists():
                out.append(str(tp))
    return out


# ── tasks ────────────────────────────────────────────────────────────────────
def task_counts(project_path: Path) -> "dict":
    """{"open": [(num, status)], "parked_open": int} — open = any non-done task."""
    open_: "list[tuple[int, str]]" = []
    parked = 0
    try:
        from tasks.core import _extract_status, _iter_task_dirs, scan_parked
        for num, _slug, tf in _iter_task_dirs(Path(project_path)):
            st = _extract_status(tf)
            if not st.startswith("done"):
                open_.append((num, st.split()[0] if st else "unknown"))
        parked = len(scan_parked(Path(project_path), open_only=True))
    except Exception:
        pass
    return {"open": open_, "parked_open": parked}


def plugin_version() -> str:
    try:
        from tasks.hooks_check import _code_version
        return _code_version() or "unknown"
    except Exception:
        return "unknown"


def verify_command(project_path: Path) -> "list[str]":
    """The EFFECTIVE close-time verify commands, resolved the way the close does
    (`resolve_verify_commands` over ONE config snapshot — plan-panel codex#3): a
    single line when every risk class runs the same list, else one line per
    risk. `[]` declared → the honest 'none declared' line."""
    try:
        from tasks.core import load_config, resolve_verify_commands
        cfg = load_config(Path(project_path))
        per = {r: [_one_line(c) for _src, c in resolve_verify_commands(Path(project_path), r, cfg=cfg)]
               for r in ("reversible", "assertive", "irreversible")}
    except Exception as e:
        return [f"verify: (config unreadable — {_one_line(e)})"]
    if all(not v for v in per.values()):
        return ["verify: (none declared — set `verify` in .agent/config.json; a close then warns and allows)"]
    if len({tuple(v) for v in per.values()}) == 1:
        return ["verify: " + " && ".join(per["reversible"])]
    return ["verify (effective per risk class):"] + [
        f"  {r}: " + (" && ".join(v) if v else "(none)") for r, v in per.items()]


# ── rendering ────────────────────────────────────────────────────────────────
def render_triggers(triggers: "list[dict]") -> "list[str]":
    if not triggers:
        return ["  none fired"]
    lines = []
    for i, t in enumerate(triggers, 1):
        lines.append(f"  [{i}] {t['kind']} · {t['seat']} — {t['detail']}")
        lines.append(f"      act: {t['command']}")
    return lines


def render_dashboard(project_path: Path, *, now: "datetime | None" = None,
                     detect: bool = True) -> str:
    now = now or datetime.now(timezone.utc)
    p = Path(project_path)
    panel = panel_seats(p)
    jp = journal_path(p)
    records, skipped, jstatus = load_review_journal(jp)
    win_start, win_label = window_bounds(now, panel_changed_at(p))
    cur = seat_stats(_window(records, win_start, now))
    knobs = review_knobs(p)
    hooks = hooks_health(p)
    tasks = task_counts(p)
    bench = _exams_with_fallback(p)
    lanes = other_lane_journals(p, jp)

    detect_report = None
    detect_note = "skipped (--no-detect)"
    if detect:
        try:
            from tasks.models_check import detect_providers
            detect_report = detect_providers(p)
            detect_note = "dead pin or new id since the catalog baseline; codex/grok/claude listed"
        except Exception as e:
            detect_note = f"provider listing failed ({_one_line(e)})"

    # new-id verdicts compare with the PREVIOUS record; the record is refreshed after
    baseline = load_catalog_baseline(catalog_baseline_path(p)) if detect_report else None
    triggers = (drift_triggers(records, now, panel, start=win_start)
                + model_gap_triggers(detect_report, panel, baseline)
                + template_triggers(bench))
    gap_info = model_gap_info(detect_report, panel)
    baseline_note = record_catalog_baseline(p, detect_report, now) if detect else ""

    L: "list[str]" = []
    L.append("=== PLAYBOOK DASHBOARD — nothing here changes a setting (read-only, except the model-catalog baseline it records) ===")
    L.append(f"as of: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append(f"plugin: {plugin_version()}")
    L.extend(verify_command(p))
    L.append("")
    L.append(f"panel ({panel['source']}) · default judge: {panel['default_judge'] or '(none)'} · "
             f"panel required for: {', '.join(panel['required_for']) or '(off)'} · window: {win_label}")
    seat_rows = []
    labels_in_panel = set()
    for s in panel["seats"]:
        labels_in_panel.add(s["label"])
        st = cur.get(s["label"])
        seat_rows.append((s["spec"], s["label"], s["effort"], st, s["error"]))
    for label in sorted(cur):
        if label not in labels_in_panel:
            seat_rows.append(("(not in panel)", label, "", cur[label], ""))
    if not seat_rows:
        L.append("  (no panel seats configured)")
    else:
        w = max(len(r[1]) for r in seat_rows)
        L.append(f"  {'seat':<{w}}  {'effort':<18} {'runs':>8}  {'ok':>5}  {'timeout':>7}  {'median':>7}  spec")
        for spec, label, effort, st, err in seat_rows:
            if st:
                cells = (f"{st['runs']:>8}  {pct(st['ok'], st['runs']):>5}  "
                         f"{pct(st['timeout'], st['runs']):>7}  {fmt_ms(st['median_ms']):>7}")
            else:
                cells = f"{'0':>8}  {'n/a':>5}  {'n/a':>7}  {'n/a':>7}"
            tail = f"  {spec}" + (f"  ⚠ {err}" if err else "")
            L.append(f"  {label:<{w}}  {effort:<18} {cells}{tail}")
    jdesc = {"ok": f"{len(records)} review records total",
             "truncated": f"TRUNCATED to the newest {_JOURNAL_READ_CAP >> 20} MB — {len(records)} review records read, older ones NOT counted",
             "missing": "no journal file yet (no review has run in this lane)",
             "unreadable": "UNREADABLE (not a regular file, or permission denied) — stats above are empty, not zero",
             "unresolved": "lane unresolvable (fresh clone without .agent/current_user) — stats above are empty, not zero"}[jstatus]
    L.append(f"  review-spend journal: {jp.relative_to(p).as_posix() if jp and jp.is_relative_to(p) else (jp or '(lane unresolvable)')} · "
             + jdesc + (f" · {skipped} malformed skipped" if skipped else "")
             + (f" · other lanes with journals (NOT aggregated): {', '.join(lanes)}" if lanes else ""))
    L.append("")
    soft = "unlimited" if knobs["soft"] is None else f"{knobs['soft']}s"
    hard = "unlimited" if knobs["hard"] is None else f"{knobs['hard']}s"
    L.append(f"review knobs: soft timeout {soft} · hard timeout {hard} · judge budget ${knobs['budget_usd']} (claude only)")
    if hooks["ok"]:
        L.append(f"hooks (doctor's hook checks: {hooks['covers']}): OK on these checks · copy {hooks['copy'] or '?'}" + (f" v{hooks['version']}" if hooks["version"] else ""))
    else:
        L.append(f"hooks (doctor's hook checks: {hooks['covers']}): {len(hooks['warnings'])} warning(s) — run: tasks doctor")
        for wmsg in hooks["warnings"][:5]:
            L.append(f"  ⚠ {wmsg}")
    open_desc = ", ".join(f"{n:03d}({st})" for n, st in tasks["open"]) or "none"
    L.append(f"tasks: {len(tasks['open'])} open [{open_desc}] · {tasks['parked_open']} open parked item(s) — tasks parked")
    L.append("")
    if bench is None:
        L.append("judgebench: not present (dev-only harness; no bench/corpus/corpus.json in the project or its code_roots, no report copy in a task record)")
    else:
        corpus = f"corpus v{bench['corpus_version']} ({bench['cases']} cases)" if bench["corpus_version"] is not None else "corpus: not present here"
        tpl = (f"exam template sha {bench['template_sha_now'][:12]} (bench instrument, not the live review prompt)"
               if bench["template_sha_now"] else "exam template: not present here")
        L.append(f"judgebench: {corpus} · {tpl}")
        if not bench["exams"]:
            L.append("  exams: none (no live-mode run under bench/runs/ and no report copy in a task record)")
        for e in bench["exams"]:
            wt = " vs ".join(f"{lab} {e['weighted'][lab]}" for lab in e["labels"] if lab in e["weighted"])
            tpl_part = (f"template {e['template_version'] or '?'} {e['template_sha'][:12]}" if e.get("template_sha")
                        else "template sha n/a (record has no manifest)")
            L.append(f"  exam {e['run_id']} · {_date(e['date'])} · {' vs '.join(e['labels'])} · {tpl_part} · "
                     f"weighted {wt or 'n/a'} · {e['verdict']} · source: {e.get('source', '?')}")
    L.append("")
    L.append(f"triggers (3 kinds: drift window-vs-prior-30d · model-gap [{detect_note}] · exam-template-vs-last-exam):")
    L.extend(render_triggers(triggers))
    if gap_info:
        L.append(f"  info: available but not seated — {' · '.join(gap_info)}   (not a trigger: a selective panel is a choice)")
    if baseline_note:
        L.append(f"  {baseline_note}")
    return "\n".join(L)


def panel_health_lines(project_path: Path, *, now: "datetime | None" = None) -> "list[str]":
    """EXACTLY six lines for `tasks bootstrap` — offline (no provider listing),
    so the model-gap trigger is deferred to the dashboard by name."""
    now = now or datetime.now(timezone.utc)
    p = Path(project_path)
    panel = panel_seats(p)
    records, _skipped, jstatus = load_review_journal(journal_path(p))
    win_start, win_label = window_bounds(now, panel_changed_at(p))
    cur = seat_stats(_window(records, win_start, now))
    bench = _exams_with_fallback(p)
    drift = drift_triggers(records, now, panel, start=win_start)
    tpl = template_triggers(bench)

    l1 = f"=== PANEL HEALTH ({win_label}) ==="
    l2 = (f"panel: {', '.join(panel['panel']) or '(none)'} · default judge: {panel['default_judge'] or '(none)'} · "
          f"required for: {', '.join(panel['required_for']) or '(off)'}")
    runs = sum(s["runs"] for s in cur.values())
    if jstatus == "truncated":
        drift = []               # windows may be incomplete — no drift verdict (r4 codex-med#2)
    if runs:
        ok = sum(s["ok"] for s in cur.values())
        to = sum(s["timeout"] for s in cur.values())
        meds = [s["median_ms"] for s in cur.values() if s["median_ms"] is not None]
        l3 = (f"reviews: {runs} judge runs across {len(cur)} seat(s) · ok {pct(ok, runs)} · timeout {pct(to, runs)} · "
              f"median of seat medians {fmt_ms(int(statistics.median(meds))) if meds else 'n/a'}"
              + (" · journal TRUNCATED — counts incomplete" if jstatus == "truncated" else ""))
        slow = max(cur.items(), key=lambda kv: (kv[1]["median_ms"] or -1))
        worst = max(cur.items(), key=lambda kv: (kv[1]["timeout"] / kv[1]["runs"] if kv[1]["runs"] else 0))
        silent = [s["label"] for s in panel["seats"] if s["label"] not in cur]
        l4 = (f"slowest seat: {slow[0]} {fmt_ms(slow[1]['median_ms'])} · most timeouts: {worst[0]} "
              f"{worst[1]['timeout']}/{worst[1]['runs']}" + (f" · no runs: {', '.join(silent)}" if silent else ""))
    else:
        why = {"missing": " (no journal yet)", "unreadable": " (journal UNREADABLE)",
               "unresolved": " (lane unresolvable)", "truncated": " (journal truncated)"}.get(jstatus, "")
        where = f"in the {win_label}" if win_label.startswith("last") else f"since the panel change ({win_start.strftime('%Y-%m-%d')})"
        l3 = f"reviews: no review-spend records {where}{why}"
        l4 = "slowest seat: n/a · most timeouts: n/a"
    if bench and bench["exams"]:
        e = bench["exams"][0]                                   # newest by date (merged list is sorted)
        with_sha = next((x for x in bench["exams"] if x.get("template_sha")), None)
        if tpl:
            tpl_part = f"exam template CHANGED since exam {tpl[0]['seat']}"
        elif with_sha and bench.get("template_sha_now"):
            tpl_part = f"exam template unchanged since exam {with_sha['run_id']} ({_date(with_sha['date'])})"
        else:
            tpl_part = f"last exam {e['run_id']} ({_date(e['date'])}) — template baseline n/a (record only)"
    else:
        tpl_part = "template: no live exam on record"
    drift_part = ("drift n/a (journal truncated)" if jstatus == "truncated" else f"drift {len(drift)} fired")
    l5 = (f"triggers (offline): {drift_part} · {tpl_part} · model-gap: needs the provider listing — see dashboard")
    l6 = "full picture + exact commands: tasks dashboard"
    return [l1, l2, l3, l4, l5, l6]


def cmd_dashboard(cmd_args) -> None:
    """The `tasks dashboard [--no-detect]` arm. Read-only by construction."""
    args = list(cmd_args or [])
    detect = "--no-detect" not in args
    project_path = find_project_root()
    print(render_dashboard(project_path, detect=detect))
