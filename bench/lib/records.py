"""Run persistence: result JSONL, manifest, run lock, bench spend journal (plan
§9 manifest, §11 records, §16 layout, §18 resume; step 6).

    bench/runs/<run-id>/
      manifest.json              reproducibility metadata (+ per-case content hashes)
      .lock                      exclusive run lock (pid) while a `run` is in flight
      <label>/result.jsonl       one line per case for that candidate
      <label>/raw/<case-id>.txt  the judge's raw output
      journal/enforcement.jsonl  spend records via production pb_journal.append_review

Resume semantics: a (case, candidate) pair is DONE iff a parseable result line
exists for it. A torn/non-JSON line (crash mid-append) is treated as ABSENT and
counted, so the pair re-runs once. Concurrent launchers are refused by the lock
(claim/check atomicity is not provided by O_APPEND — panel F6).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path

from bench.lib import REPO_ROOT, PLUGIN_ROOT

LOCK_NAME = ".lock"
MANIFEST_NAME = "manifest.json"


class RunLocked(RuntimeError):
    pass


class ManifestMismatch(ValueError):
    pass


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


# ── lock ─────────────────────────────────────────────────────────────────────

class RunLock:
    """`with RunLock(run_dir):` — O_CREAT|O_EXCL so two launchers can never both
    hold it. The file carries the holder's pid for the refusal message."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / LOCK_NAME
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = ""
            try:
                holder = self.path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            raise RunLocked(f"run is locked by another launcher (pid {holder or '?'}): "
                            f"{self.path} — wait for it, or delete the lock if that "
                            f"process is gone") from None
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *exc):
        try:
            if self._fd is not None:
                os.close(self._fd)
        finally:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


# ── results ──────────────────────────────────────────────────────────────────

def candidate_dir(run_dir: Path, label: str) -> Path:
    return Path(run_dir) / label


def result_path(run_dir: Path, label: str) -> Path:
    return candidate_dir(run_dir, label) / "result.jsonl"


def append_result(run_dir: Path, label: str, record: dict) -> None:
    p = result_path(run_dir, label)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(str(p), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def write_raw(run_dir: Path, label: str, case_id: str, raw: str) -> str:
    d = candidate_dir(run_dir, label) / "raw"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{case_id}.txt"
    p.write_text(raw or "", encoding="utf-8", errors="replace")
    return p.relative_to(run_dir).as_posix()


def read_results(run_dir: Path, label: str) -> tuple:
    """(records, torn) — every parseable JSON object line; `torn` counts the
    lines that were not (a crash mid-append leaves one)."""
    p = result_path(run_dir, label)
    if not p.is_file():
        return [], 0
    recs, torn = [], 0
    for line in p.read_text(encoding="utf-8", errors="replace").split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            torn += 1
            continue
        if isinstance(obj, dict) and obj.get("case_id") and obj.get("label"):
            recs.append(obj)
        else:
            torn += 1
    return recs, torn


def completed_pairs(run_dir: Path, labels) -> tuple:
    """({(case_id, label)}, torn_total) over every candidate's result file."""
    done, torn_total = set(), 0
    for label in labels:
        recs, torn = read_results(run_dir, label)
        torn_total += torn
        for r in recs:
            done.add((r["case_id"], r["label"]))
    return done, torn_total


def all_results(run_dir: Path) -> dict:
    """label → [records] for every candidate dir present (report/adjudicate input)."""
    out = {}
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return out
    for d in sorted(p for p in run_dir.iterdir() if p.is_dir() and p.name != "journal"):
        recs, _ = read_results(run_dir, d.name)
        if recs:
            out[d.name] = recs
    return out


def make_result_record(run_id: str, case, candidate, invocation, raw_rel: str, package) -> dict:
    return {
        "ts": utcnow(),
        "run_id": run_id,
        "case_id": case.id,
        "label": candidate.label,
        "spec": candidate.spec,
        "backend": candidate.backend,
        "variant": candidate.variant,
        "status": invocation.status,
        "duration_ms": invocation.duration_ms,
        "retries": invocation.retries,
        "usage": invocation.usage,
        "findings": invocation.findings.to_dict() if invocation.findings else None,
        "raw_path": raw_rel,
        "note": invocation.note,
        "template_version": package.template_version,
        "template_sha256": package.template_sha256,
        "prompt_sha256": sha256_text(package.prompt),
    }


# ── manifest ─────────────────────────────────────────────────────────────────

def case_hashes(case, package) -> dict:
    ctx = {}
    for p in case.context_files():
        ctx[p.relative_to(case.path).as_posix()] = sha256_file(p)
    return {"spec_md": sha256_file(case.spec_path), "diff_patch": sha256_file(case.diff_path),
            "context": ctx, "prompt": sha256_text(package.prompt),
            "truth_version": case.truth_version}


def cli_version(backend: str) -> str:
    """`<binary> --version` first line, best-effort; 'unavailable' when absent."""
    try:
        from provider.subagent import _adapter_class
        binary = _adapter_class(backend)(session_id="bench", project_root=REPO_ROOT).binary_name()
    except Exception:
        binary = backend
    try:
        p = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace")
        line = (p.stdout or p.stderr or "").strip().splitlines()
        return line[0] if line else f"exit {p.returncode}"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def git_head(repo: Path) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=20, encoding="utf-8")
        return p.stdout.strip() if p.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def sandbox_mode() -> str:
    try:
        from provider import sandbox as _sb
        return "read-only contained" if _sb.containment_available() else "UNCONTAINED (no OS sandbox)"
    except Exception:
        return "unknown"


def build_manifest(*, run_id, mode, corpus, selected_cases, packages, candidates, soft_timeout,
                   hard_timeout, concurrency, source_repos, template_version, template_sha256,
                   fake_script=None) -> dict:
    return {
        "run_id": run_id,
        "mode": mode,                                   # fake | live
        "created_at": utcnow(),
        "host": {"os": platform.system(), "release": platform.release(),
                 "python": platform.python_version()},
        "playbook_repo_sha": git_head(REPO_ROOT),
        "corpus": {"root": str(corpus.root), "version": corpus.version,
                   "cases": [c.id for c in selected_cases],
                   "hashes": {c.id: case_hashes(c, packages[c.id]) for c in selected_cases}},
        "source_repos": {k: {"path": str(v), "head": git_head(Path(v))} for k, v in source_repos.items()},
        "template": {"version": template_version, "sha256": template_sha256},
        "candidates": [dict(c.to_dict(), cli_version=("fake" if mode == "fake" else cli_version(c.backend)))
                       for c in candidates],
        "timeouts": {"soft_secs": soft_timeout, "hard_secs": hard_timeout},
        "concurrency": concurrency,
        "sandbox": sandbox_mode() if mode == "live" else "none (fake runner)",
        "web_search": False,
        "retry_policy": ("one retry on transport-class failure only: '(error:' spawn/resolution "
                         "(not 'not found on PATH'), or '(FAILED — exit N)' with no output; "
                         "never on content"),
        "fake_script": str(fake_script) if fake_script else None,
        "manual_quota_notes": "",
    }


def write_manifest(run_dir: Path, manifest: dict) -> None:
    from tasks.atomic import atomic_write
    atomic_write(Path(run_dir) / MANIFEST_NAME,
                 json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def read_manifest(run_dir: Path) -> dict:
    p = Path(run_dir) / MANIFEST_NAME
    return json.loads(p.read_text(encoding="utf-8"))


def check_manifest(manifest: dict, *, selected_cases, packages, candidates) -> list:
    """Mismatches between a stored manifest and the corpus/candidates now — a
    resume must refuse on any (panel F9: same run id, same inputs, or nothing)."""
    problems = []
    stored = manifest.get("corpus", {}).get("hashes", {})
    for c in selected_cases:
        now = case_hashes(c, packages[c.id])
        was = stored.get(c.id)
        if was is None:
            problems.append(f"case {c.id}: not in the run manifest (was the case list changed?)")
            continue
        for k in ("spec_md", "diff_patch", "context", "prompt", "truth_version"):
            if was.get(k) != now.get(k):
                problems.append(f"case {c.id}: {k} changed since the run started")
    stored_c = {c["label"]: c for c in manifest.get("candidates", [])}
    for cand in candidates:
        was = stored_c.get(cand.label)
        if was is None:
            problems.append(f"candidate {cand.label}: not in the run manifest")
        elif was.get("spec") != cand.spec:
            problems.append(f"candidate {cand.label}: spec {was.get('spec')!r} → {cand.spec!r}")
    return problems


# ── spend journal (production envelope, bench-local sink) ────────────────────

_PBJ = None


def _pb_journal():
    global _PBJ
    if _PBJ is None:
        import importlib.util
        p = PLUGIN_ROOT / "scripts" / "pb_journal.py"
        spec = importlib.util.spec_from_file_location("_pb_journal_bench", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PBJ = mod
    return _PBJ


def journal_spend(run_dir: Path, *, seat: str, case_id: str, duration_ms: int, status: str,
                  usage) -> None:
    """One `hook="review"`, `kind="bench"` record into `<run-dir>/journal/enforcement.jsonl`
    via production `append_review` (never raises; the run dir is the ONLY sink —
    production lanes are never touched)."""
    try:
        _pb_journal().append_review(Path(run_dir), session_id="bench", seat=seat, task=case_id,
                                    round_no=0, kind="bench", duration_ms=duration_ms,
                                    status=status, usage=(None if not usage or
                                                          usage.get("status") != "known" else usage))
    except Exception:
        pass
