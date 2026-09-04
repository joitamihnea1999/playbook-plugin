"""Case model + corpus loader for the judge benchmark (plan §8, §10).

A corpus is a directory:

    corpus.json                 {"version": N, "cases": ["<id>", …]}   — the frozen index
    cases/<id>/case.json        metadata (schema below)
    cases/<id>/spec.md          reconstructed pre-review task spec
    cases/<id>/diff.patch       the exact reviewed diff
    cases/<id>/truth.json       ground truth from historical triage
    cases/<id>/context/         optional extra frozen artifacts

Bias guard (§8): a case exists iff it is listed in `corpus.json` AND its dir is
present — an unlisted dir or a listed-but-missing dir is an error, never
silently included/excluded. Every `CorpusError` names the offending case/file so
a corpus builder (step 9) gets a precise message. Fields the plan marks optional
(`notes`, `context/`, `known_rejects`) stay optional.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("feature", "bugfix", "refactor", "docs", "perf")
AREAS = ("enforcement", "server", "ui", "tests", "docs")
DIFFICULTIES = ("easy", "medium", "hard")
SEVERITIES = ("Critical", "Important", "Minor")           # the existing 3-level vocabulary (§5)
OUTCOMES = ("accepted+fixed", "accepted+parked",           # historical triage (§10)
            "valid-new")                                    # appended by `adjudicate` (§10)

_REQUIRED_CASE_KEYS = ("id", "source", "repo_base_sha", "diff_of", "kind", "area",
                       "difficulty", "truth_version")
_REQUIRED_FINDING_KEYS = ("id", "file", "failure_mode", "severity", "historical_outcome")
_REQUIRED_REJECT_KEYS = ("id", "claim", "why_rejected")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CorpusError(ValueError):
    """The corpus on disk violates the schema. The message names the case/file."""


@dataclass
class Case:
    id: str
    path: Path
    meta: dict
    truth: dict

    @property
    def kind(self) -> str:
        return self.meta["kind"]

    @property
    def area(self) -> str:
        return self.meta["area"]

    @property
    def difficulty(self) -> str:
        return self.meta["difficulty"]

    @property
    def truth_version(self) -> int:
        return self.meta["truth_version"]

    @property
    def repo_base_sha(self) -> str:
        return self.meta["repo_base_sha"]

    @property
    def source(self) -> dict:
        return self.meta["source"]

    @property
    def spec_path(self) -> Path:
        return self.path / "spec.md"

    @property
    def diff_path(self) -> Path:
        return self.path / "diff.patch"

    @property
    def truth_path(self) -> Path:
        return self.path / "truth.json"

    @property
    def context_dir(self) -> Path:
        return self.path / "context"

    def context_files(self) -> list:
        if not self.context_dir.is_dir():
            return []
        return sorted(p for p in self.context_dir.rglob("*") if p.is_file())

    def describe(self) -> str:
        d = dict(self.meta)
        d["_truth_findings"] = len(self.truth.get("findings", []))
        d["_known_rejects"] = len(self.truth.get("known_rejects", []))
        d["_context_files"] = [p.relative_to(self.path).as_posix() for p in self.context_files()]
        return json.dumps(d, indent=2, sort_keys=True)


@dataclass
class Corpus:
    root: Path
    version: int
    cases: list = field(default_factory=list)

    def get(self, case_id: str):
        for c in self.cases:
            if c.id == case_id:
                return c
        return None

    def ids(self) -> list:
        return [c.id for c in self.cases]


def _read_json(path: Path, where: str) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CorpusError(f"{where}: cannot read {path.name}: {exc}") from exc
    except ValueError as exc:
        raise CorpusError(f"{where}: {path.name} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise CorpusError(f"{where}: {path.name} must be a JSON object")
    return obj


def _check_enum(where: str, key: str, value, allowed) -> None:
    if value not in allowed:
        raise CorpusError(f"{where}: {key}={value!r} not one of {list(allowed)}")


def _validate_case_meta(meta: dict, case_id: str, dirname: str) -> None:
    where = f"case {dirname}"
    for k in _REQUIRED_CASE_KEYS:
        if k not in meta:
            raise CorpusError(f"{where}: case.json missing required key {k!r}")
    if meta["id"] != case_id or case_id != dirname:
        raise CorpusError(f"{where}: case.json id {meta['id']!r} must equal the index id "
                          f"{case_id!r} and the directory name {dirname!r}")
    if not isinstance(meta["source"], dict):
        raise CorpusError(f"{where}: source must be an object")
    for k in ("workspace", "task", "repo"):
        if not isinstance(meta["source"].get(k), str) or not meta["source"][k]:
            raise CorpusError(f"{where}: source.{k} must be a non-empty string")
    sha = meta["repo_base_sha"]
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise CorpusError(f"{where}: repo_base_sha must be a 7-40 char hex sha, got {sha!r}")
    if not isinstance(meta["diff_of"], str) or not meta["diff_of"].strip():
        raise CorpusError(f"{where}: diff_of must be a non-empty string (sha or range)")
    _check_enum(where, "kind", meta["kind"], KINDS)
    _check_enum(where, "area", meta["area"], AREAS)
    _check_enum(where, "difficulty", meta["difficulty"], DIFFICULTIES)
    tv = meta["truth_version"]
    if isinstance(tv, bool) or not isinstance(tv, int) or tv < 1:
        raise CorpusError(f"{where}: truth_version must be an int >= 1, got {tv!r}")
    if "notes" in meta and not isinstance(meta["notes"], str):
        raise CorpusError(f"{where}: notes must be a string when present")


def validate_truth(truth: dict, where: str, expected_version=None) -> None:
    """Validate a `truth.json` object (also used after `adjudicate` appends)."""
    findings = truth.get("findings")
    if not isinstance(findings, list):
        raise CorpusError(f"{where}: truth.json must have a 'findings' list")
    seen = set()
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            raise CorpusError(f"{where}: truth.json findings[{i}] must be an object")
        for k in _REQUIRED_FINDING_KEYS:
            if k not in f:
                raise CorpusError(f"{where}: truth.json findings[{i}] missing {k!r}")
        if f["id"] in seen:
            raise CorpusError(f"{where}: truth.json duplicate finding id {f['id']!r}")
        seen.add(f["id"])
        _check_enum(f"{where}: truth.json finding {f['id']}", "severity",
                    f["severity"], SEVERITIES)
        _check_enum(f"{where}: truth.json finding {f['id']}", "historical_outcome",
                    f["historical_outcome"], OUTCOMES)
        if "symbol" in f and f["symbol"] is not None and not isinstance(f["symbol"], str):
            raise CorpusError(f"{where}: truth.json finding {f['id']} symbol must be a string")
    rejects = truth.get("known_rejects", [])
    if not isinstance(rejects, list):
        raise CorpusError(f"{where}: truth.json known_rejects must be a list")
    rseen = set()
    for i, r in enumerate(rejects):
        if not isinstance(r, dict):
            raise CorpusError(f"{where}: truth.json known_rejects[{i}] must be an object")
        for k in _REQUIRED_REJECT_KEYS:
            if k not in r:
                raise CorpusError(f"{where}: truth.json known_rejects[{i}] missing {k!r}")
        if r["id"] in rseen or r["id"] in seen:
            raise CorpusError(f"{where}: truth.json duplicate id {r['id']!r}")
        rseen.add(r["id"])
        # Optional deterministic key (panel F2): a reject with `file` (+`symbol`)
        # can be auto-matched; one with only prose never is.
        for k in ("file", "symbol"):
            if k in r and r[k] is not None and not isinstance(r[k], str):
                raise CorpusError(f"{where}: truth.json known_rejects {r['id']} {k} must be a string")
    if "truth_version" in truth and expected_version is not None:
        if truth["truth_version"] != expected_version:
            raise CorpusError(f"{where}: truth.json truth_version {truth['truth_version']!r} "
                              f"!= case.json truth_version {expected_version!r}")
    # After `adjudicate` touches a case, truth.json carries `truth_version_pending` instead
    # (case.json is the authority): equal = consistent; one AHEAD of case.json = a crash
    # between the two writes (recoverable, tolerated); anything else is a hand-edit desync.
    pending = truth.get("truth_version_pending")
    if pending is not None and expected_version is not None:
        if not isinstance(pending, int) or pending not in (expected_version, expected_version + 1):
            raise CorpusError(f"{where}: truth.json truth_version_pending {pending!r} is inconsistent "
                              f"with case.json truth_version {expected_version!r}")


def load_case(case_dir: Path, case_id: str) -> Case:
    dirname = case_dir.name
    where = f"case {dirname}"
    if not case_dir.is_dir():
        raise CorpusError(f"{where}: directory missing for indexed case {case_id!r}")
    cj = case_dir / "case.json"
    if not cj.is_file():
        raise CorpusError(f"{where}: case.json missing")
    meta = _read_json(cj, where)
    _validate_case_meta(meta, case_id, dirname)
    for name in ("spec.md", "diff.patch", "truth.json"):
        if not (case_dir / name).is_file():
            raise CorpusError(f"{where}: {name} missing")
    truth = _read_json(case_dir / "truth.json", where)
    validate_truth(truth, where, expected_version=meta["truth_version"])
    truth.setdefault("known_rejects", [])
    return Case(id=case_id, path=case_dir, meta=meta, truth=truth)


def load_corpus(root: Path) -> Corpus:
    root = Path(root)
    if not root.is_dir():
        raise CorpusError(f"corpus dir not found: {root}")
    index_path = root / "corpus.json"
    cases_dir = root / "cases"
    if not index_path.exists():
        # An empty corpus is valid (step 1 skeleton; before step 9 builds content) —
        # but only if there are no orphan case dirs either.
        orphans = sorted(p.name for p in cases_dir.iterdir()) if cases_dir.is_dir() else []
        if orphans:
            raise CorpusError(f"no corpus.json but case dirs exist: {orphans} — every case "
                              "must be frozen in the index")
        return Corpus(root=root, version=0, cases=[])
    index = _read_json(index_path, "corpus.json")
    version = index.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CorpusError(f"corpus.json: version must be an int >= 1, got {version!r}")
    ids = index.get("cases")
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise CorpusError("corpus.json: 'cases' must be a list of case-id strings")
    seen = set()
    for cid in ids:
        if not _ID_RE.match(cid):
            raise CorpusError(f"corpus.json: invalid case id {cid!r}")
        if cid in seen:
            raise CorpusError(f"corpus.json: duplicate case id {cid!r}")
        seen.add(cid)
    on_disk = set(p.name for p in cases_dir.iterdir() if p.is_dir()) if cases_dir.is_dir() else set()
    extra = sorted(on_disk - seen)
    if extra:
        raise CorpusError(f"case dir(s) not frozen in corpus.json: {extra}")
    cases = [load_case(cases_dir / cid, cid) for cid in ids]
    return Corpus(root=root, version=version, cases=cases)


def select_cases(corpus: Corpus, spec: str) -> list:
    """`all` or a comma-separated id list (order preserved). Unknown id → error."""
    if spec.strip() == "all":
        if not corpus.cases:
            raise CorpusError(f"corpus at {corpus.root} has no cases — build the corpus first "
                              "(plan step 9) or point --corpus at one that has")
        return list(corpus.cases)
    out = []
    seen = set()
    for cid in (s.strip() for s in spec.split(",")):
        if not cid:
            continue
        if cid in seen:
            raise CorpusError(f"duplicate case id {cid!r} in --cases (a pair must never run twice)")
        seen.add(cid)
        c = corpus.get(cid)
        if c is None:
            raise CorpusError(f"unknown case id {cid!r} (known: {corpus.ids()})")
        out.append(c)
    if not out:
        raise CorpusError("no cases selected")
    return out
