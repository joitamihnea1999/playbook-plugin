"""Which plugin copies are in play — hook, launcher, installed, doctor (PLAN S3, task 086).

`tasks doctor` used to read its OWN `plugin.json` and its OWN `core.VERSION` and
report "installed=X, code=X": a copy compared with itself, PASS from every copy.
On a directory-marketplace machine three different copies run: Claude Code's hooks
execute the marketplace checkout, `.claude/bin/tasks` executes whatever
`installed_plugins.json` points at, and the doctor runs from wherever it was
invoked. This module names each one — path and version, every value read from its
own source — and says whether they are the same release.

It READS; the only code it runs is this plugin copy's own launcher resolver
(`scripts/wrapper_resolver.py`, the text every generated wrapper embeds) and `git`.
It never executes the project's `.claude/bin/tasks`: a custom or stale wrapper is
reported as "target unknown".
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

WRAPPER_PLACEHOLDER = "@@PLAYBOOK_WRAPPER_RESOLVER@@"
_TEMPLATE_OPEN = "<<'END_WRAPPER_TEMPLATE'\n"
_TEMPLATE_CLOSE = "\nEND_WRAPPER_TEMPLATE\n"


# ── reading ──────────────────────────────────────────────────────────────────

def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def manifest_version(copy: "Path | str") -> "str | None":
    data = _load_json(Path(copy) / ".claude-plugin" / "plugin.json")
    v = data.get("version") if isinstance(data, dict) else None
    return v if isinstance(v, str) and v else None


def _git(cwd: "Path | str", *args: str) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def _same_path(a: "str | Path", b: "str | Path") -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


# ── the launcher: template + resolver of THIS copy ───────────────────────────

def wrapper_template(doctor_root: Path, name: str = "tasks") -> "str | None":
    """The exact text `create_wrapper` writes for `name`, rebuilt from this copy's
    `gate-echo-lib.sh` + `wrapper_resolver.py` without running bash (the bash side
    is `$(cat <<'END_WRAPPER_TEMPLATE' …)` — trailing newlines stripped — spliced,
    substituted, then written with `printf '%s\\n'`)."""
    try:
        lib = (doctor_root / "scripts" / "gate-echo-lib.sh").read_text(encoding="utf-8")
        start = lib.index(_TEMPLATE_OPEN) + len(_TEMPLATE_OPEN)
        body = lib[start:lib.index(_TEMPLATE_CLOSE, start)]
        if WRAPPER_PLACEHOLDER in body:
            resolver = (doctor_root / "scripts" / "wrapper_resolver.py").read_text(encoding="utf-8")
            head, _, tail = body.partition(WRAPPER_PLACEHOLDER)
            body = head + resolver.rstrip("\n") + tail
    except (OSError, ValueError):
        return None
    return body.rstrip("\n").replace("WRAPPER_NAME", name) + "\n"


def resolve_launcher_script(doctor_root: Path, project: Path, home: Path) -> "str | None":
    """What a CURRENT managed wrapper in `project` would exec: this copy's resolver,
    run exactly as the wrapper runs it (stdin, argv[1] = the physical project root,
    `$HOME` pointing at `home`)."""
    try:
        code = (doctor_root / "scripts" / "wrapper_resolver.py").read_text(encoding="utf-8")
    except OSError:
        return None
    code = code.replace("WRAPPER_NAME", "tasks")
    env = dict(os.environ, HOME=str(home))
    try:
        # BYTES, UTF-8: `python -` decodes its source as UTF-8, while a text pipe
        # would encode it in the locale code page (cp1252 on Windows turned the
        # resolver's em dash into 0x97 → SyntaxError → nothing resolved; CI
        # run 35970907673). The wrapper passes the file's own bytes the same way.
        r = subprocess.run([sys.executable, "-", os.path.realpath(str(project))],
                           input=code.encode("utf-8"), capture_output=True, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.decode("utf-8", "surrogateescape").strip()
    return out or None


# ── the installed entry the launcher selected, and the hook copy ─────────────

def _installed_entries(home: Path) -> "list[tuple[str, dict]]":
    data = _load_json(home / ".claude" / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    out = []
    for key, entries in (plugins or {}).items():
        if not isinstance(key, str) or key.split("@")[0] != "playbook":
            continue
        for e in entries or []:
            if isinstance(e, dict) and e.get("installPath"):
                out.append((key, e))
    return out


def selected_entry(home: Path, script: "str | None") -> "tuple[tuple[str, dict] | None, str]":
    """(entry, problem) — the installed entry whose `installPath` the resolver's answer
    came from, matched the way the resolver builds it (`installPath + "/scripts/tasks"`).
    User- and project-scoped entries may share one cache path; they must then agree
    on version and sha AND belong to ONE marketplace key (two keys could name two
    different hook trees) — otherwise the selection is ambiguous."""
    if not script:
        return None, "the launcher resolver selected no copy"
    hits = [(k, e) for k, e in _installed_entries(home)
            if e["installPath"] + "/scripts/tasks" == script]
    if not hits:
        return None, "no playbook entry in installed_plugins.json matches the launcher resolver's choice"
    if len({k for k, _ in hits}) != 1 or len({(e.get("version"), e.get("gitCommitSha")) for _, e in hits}) != 1:
        return None, (f"ambiguous: {len(hits)} installed entries under "
                      f"{', '.join(sorted({k for k, _ in hits}))} share {hits[0][1]['installPath']}")
    return hits[0], ""


def hook_copy_path(home: Path, key: str, entry: dict) -> str:
    """What Claude Code's hooks execute for this entry: a `directory` marketplace
    runs its checkout (`installLocation` + the plugin's `source`), anything else
    the entry's `installPath`."""
    market = key.split("@", 1)[1] if "@" in key else ""
    mk = _load_json(home / ".claude" / "plugins" / "known_marketplaces.json")
    m = mk.get(market) if isinstance(mk, dict) else None
    if not isinstance(m, dict):
        return entry["installPath"]
    src = m.get("source") or {}
    loc = m.get("installLocation")
    if isinstance(src, dict) and src.get("source") == "directory" and isinstance(loc, str) and loc:
        mp = _load_json(Path(loc) / ".claude-plugin" / "marketplace.json")
        for p in (mp or {}).get("plugins") or []:
            if isinstance(p, dict) and p.get("name") == "playbook" and isinstance(p.get("source"), str):
                return os.path.normpath(os.path.join(loc, p["source"]))
    return entry["installPath"]


# ── content: the installed copy against the release commit ───────────────────

def _is_runtime_artifact(rel: str) -> bool:
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith(".pyc")


def _sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _blob_shas(path: Path, autocrlf: bool) -> "set[str]":
    """The blob sha(s) git would accept for this file: its raw bytes, plus — when
    the hook repo converts line endings (`core.autocrlf` true/input) and the file
    is text — the LF-normalised bytes, exactly what git's clean step stores."""
    if path.is_symlink():
        return {_sha(os.readlink(path).encode("utf-8", "surrogateescape"))}
    data = path.read_bytes()
    shas = {_sha(data)}
    if autocrlf and b"\r\n" in data and b"\0" not in data:
        shas.add(_sha(data.replace(b"\r\n", b"\n")))
    return shas


def content_differences(hook: str, install_path: str, sha: str) -> "list[str]":
    """The installed files against `<sha>:<hook's path in its repo>`, both ways, blob by
    blob, objects read from the hook copy's repository. `__pycache__/` and `*.pyc`
    are runtime artifacts and ignored."""
    prefix = _git(hook, "rev-parse", "--show-prefix")
    if prefix is None or prefix.returncode != 0:
        return ["installed content unverified — the hook copy is not a git checkout"]
    pre = prefix.stdout.decode("utf-8", "surrogateescape").strip()
    tree = _git(hook, "ls-tree", "-r", "-z", "--full-tree", sha, "--", pre or ".")
    if tree is None or tree.returncode != 0:
        return [f"installed content unverified — release commit {sha[:7]} not in the hook copy's repo"]
    release, modes = {}, {}
    for rec in tree.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        rel = path[len(pre):] if pre and path.startswith(pre) else path
        fields = meta.split()
        if len(fields) == 3 and fields[1] == "blob" and not _is_runtime_artifact(rel):
            release[rel], modes[rel] = fields[2], fields[0]
    cfg = _git(hook, "config", "--get", "core.autocrlf")
    autocrlf = bool(cfg and cfg.returncode == 0
                    and cfg.stdout.strip().lower() in (b"true", b"input"))
    installed: "dict[str, set[str]]" = {}
    root = Path(install_path)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            if _is_runtime_artifact(rel):
                continue
            try:
                installed[rel] = _blob_shas(full, autocrlf)
            except OSError:
                installed[rel] = {"<unreadable>"}
    diffs = []
    missing = sorted(set(release) - set(installed))
    extra = sorted(set(installed) - set(release))
    changed = sorted(r for r in set(release) & set(installed) if release[r] not in installed[r])
    # the execute bit decides which copy a wrapper selects (`os.access(…, X_OK)`);
    # Windows has no POSIX execute bit to compare
    moded = []
    if os.name != "nt":
        for r in sorted(set(release) & set(installed)):
            if modes[r] in ("100644", "100755"):
                try:
                    x = bool((root / r).stat().st_mode & 0o111)
                except OSError:
                    continue
                if x != (modes[r] == "100755"):
                    moded.append(r)
    for label, items in (("missing from the install", missing), ("not in the release", extra),
                         ("differs from the release", changed), ("mode differs from the release", moded)):
        if items:
            shown = ", ".join(items[:3]) + (f" (+{len(items) - 3} more)" if len(items) > 3 else "")
            diffs.append(f"{len(items)} file(s) {label}: {shown}")
    return diffs


# ── the report ───────────────────────────────────────────────────────────────

def _git_state(copy: str) -> "tuple[str | None, bool | None]":
    """(HEAD sha, clean?) for a copy inside a git checkout; (None, None) outside."""
    head = _git(copy, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return None, None
    status = _git(copy, "status", "--porcelain", "--", ".")
    clean = None if status is None or status.returncode != 0 else not status.stdout.strip()
    return head.stdout.decode().strip(), clean


def report(project: Path, *, home: "Path | None" = None,
           doctor_root: "Path | None" = None) -> "list[tuple[str, str]]":
    """[(tag, text)] — four `plugin: <label> copy — <path> v<version>` lines, then the
    `plugin: copies agree` verdict (PASS or WARN, never FAIL: a disagreement is a
    state to see, not a broken install)."""
    home = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    doctor_root = Path(doctor_root) if doctor_root is not None else Path(__file__).resolve().parent.parent
    lines: "list[tuple[str, str]]" = []
    diffs: "list[str]" = []

    script = resolve_launcher_script(doctor_root, project, home)
    sel, problem = selected_entry(home, script)

    # hook + installed (both hang on the selected entry)
    hook = None
    if sel is None:
        lines.append(("WARN", f"plugin: installed copy — {problem}"))
        diffs.append(problem)
    else:
        key, entry = sel
        hook = hook_copy_path(home, key, entry)
        hv = manifest_version(hook)
        head, clean = _git_state(hook)
        tail = f" (HEAD {head[:7]}{'' if clean else ', dirty'})" if head else ""
        lines.append(("INFO", f"plugin: hook copy — {hook} v{hv or '?'}{tail}"))
        sha = entry.get("gitCommitSha") or ""
        iv = entry.get("version") or "?"
        lines.append(("INFO", f"plugin: installed copy — {entry['installPath']} v{iv}"
                              + (f" (sha {sha[:7]})" if sha else "")))
        if head:
            if clean is not True:
                diffs.append("the hook copy has uncommitted changes")
            if sha and head != sha:
                diffs.append(f"hook copy HEAD {head[:7]} != installed sha {sha[:7]}")
        elif hv != iv:
            diffs.append(f"hook copy v{hv} != installed v{iv}")
        ivm = manifest_version(entry["installPath"])
        for label, v in (("hook", hv), ("installed", ivm)):
            if v != iv:
                diffs.append(f"{label} copy manifest v{v} != installed entry v{iv}")
        if sha:
            diffs.extend(content_differences(hook, entry["installPath"], sha))
        else:
            diffs.append("installed entry has no gitCommitSha — content unverified")

    # launcher (read, never executed)
    wrapper = project / ".claude" / "bin" / "tasks"
    launcher = None
    try:
        wtext = wrapper.read_text(encoding="utf-8")
    except OSError:
        wtext = None
    if wtext is None:
        lines.append(("WARN", f"plugin: launcher copy — no {wrapper}"))
        diffs.append("no project launcher")
    elif "# playbook-managed" not in wtext:
        lines.append(("WARN", f"plugin: launcher copy — custom launcher — not executed, target unknown ({wrapper})"))
        diffs.append("custom launcher")
    elif wtext != wrapper_template(doctor_root, "tasks"):
        lines.append(("WARN", f"plugin: launcher copy — stale launcher — target unknown, not executed ({wrapper})"))
        diffs.append("stale launcher")
    elif not script:
        lines.append(("WARN", "plugin: launcher copy — the resolver selected no copy"))
        diffs.append("launcher resolves to nothing")
    else:
        launcher = os.path.dirname(os.path.dirname(script))
        lv = manifest_version(launcher)
        lines.append(("INFO", f"plugin: launcher copy — {launcher} v{lv or '?'}"))
        if sel is not None:
            if lv != (sel[1].get("version") or "?"):
                diffs.append(f"launcher copy manifest v{lv} != installed entry v{sel[1].get('version')}")
            if not _same_path(launcher, sel[1]["installPath"]):
                diffs.append("launcher path != installPath")

    # doctor
    dv = manifest_version(doctor_root)
    lines.append(("INFO", f"plugin: doctor copy — {doctor_root} v{dv or '?'}"))
    known = [p for p in (hook, launcher, sel[1]["installPath"] if sel else None) if p]
    if not any(_same_path(doctor_root, p) for p in known):
        diffs.append("the doctor runs from a fourth copy")

    if diffs:
        lines.append(("WARN", "plugin: copies agree — NO: " + "; ".join(diffs)))
    else:
        lines.append(("PASS", "plugin: copies agree — hook, launcher and installed copies are one release"))
    return lines
