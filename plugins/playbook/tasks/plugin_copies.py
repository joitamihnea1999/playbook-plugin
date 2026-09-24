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
        # `-I` (isolated): no cwd on sys.path, no PYTHON* env — a project file
        # named glob.py or json.py must not run inside the doctor (impl panel).
        r = subprocess.run([sys.executable, "-I", "-", os.path.realpath(str(project))],
                           input=code.encode("utf-8"), capture_output=True, env=env,
                           cwd=str(doctor_root), timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.decode("utf-8", "surrogateescape").strip()
    return out or None


# ── the installed entry the launcher selected, and the hook copy ─────────────

def _installed_entries(home: Path) -> "list[tuple[str, dict]]":
    data = _load_json(home / ".claude" / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    out = []
    if not isinstance(plugins, dict):            # valid JSON of the wrong shape → no entries
        return out
    for key, entries in plugins.items():
        if not isinstance(key, str) or key.split("@")[0] != "playbook" or not isinstance(entries, list):
            continue
        for e in entries:
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


def _marketplace(home: Path, key: str) -> "tuple[dict | None, str]":
    """(marketplace entry, problem) for an installed key `playbook@<market>`."""
    market = key.split("@", 1)[1] if "@" in key else ""
    mk = _load_json(home / ".claude" / "plugins" / "known_marketplaces.json")
    if not isinstance(mk, dict):
        return None, "known_marketplaces.json is missing or unreadable"
    m = mk.get(market)
    if not isinstance(m, dict):
        return None, f"marketplace {market!r} is not in known_marketplaces.json"
    return m, ""


def _plugin_source(loc: str) -> "str | None":
    """The playbook plugin's `source` inside a marketplace checkout, or None."""
    mp = _load_json(Path(loc) / ".claude-plugin" / "marketplace.json")
    plist = mp.get("plugins") if isinstance(mp, dict) else None
    for p in plist if isinstance(plist, list) else []:
        if isinstance(p, dict) and p.get("name") == "playbook" and isinstance(p.get("source"), str):
            return p["source"]
    return None


def hook_copy_path(home: Path, key: str, entry: dict) -> "tuple[str | None, str]":
    """(path, problem) — what Claude Code's hooks execute for this entry: a
    `directory` marketplace runs its checkout (`installLocation` + the plugin's
    `source`); any other marketplace runs the entry's `installPath`. Missing or
    unreadable marketplace metadata makes the hook copy UNKNOWN (None), never a
    guess — the hooks of a directory marketplace do not run the installPath."""
    m, problem = _marketplace(home, key)
    if m is None:
        return None, problem
    src, loc = m.get("source"), m.get("installLocation")
    if not isinstance(src, dict) or not src.get("source"):
        return None, "the marketplace entry has no source kind"
    if src.get("source") != "directory":
        return entry["installPath"], ""
    if not (isinstance(loc, str) and loc):
        return None, "the directory marketplace has no installLocation"
    rel = _plugin_source(loc)
    if rel is None:
        return None, f"no playbook plugin in {loc}/.claude-plugin/marketplace.json"
    return os.path.normpath(os.path.join(loc, rel)), ""


# ── content: the installed copy against the release commit ───────────────────

def _is_runtime_artifact(rel: str) -> bool:
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith(".pyc")


def _sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _release_repo(hook: "str | None", home: Path, key: str) -> "tuple[str | None, str, str]":
    """(repo dir, path prefix of the plugin inside it, problem): where the release
    commit's objects are read. The hook copy when it is a git checkout (a directory
    marketplace); otherwise the marketplace's own clone (`installLocation`, which
    Claude Code keeps as a git repository) at the plugin's `source`."""
    if hook:
        pre = _git(hook, "rev-parse", "--show-prefix")
        if pre is not None and pre.returncode == 0:
            return hook, pre.stdout.decode("utf-8", "surrogateescape").strip(), ""
    m, problem = _marketplace(home, key)
    loc = m.get("installLocation") if m else None
    if isinstance(loc, str) and loc:
        top = _git(loc, "rev-parse", "--show-prefix")
        rel = _plugin_source(loc)
        if top is not None and top.returncode == 0 and rel is not None:
            base = top.stdout.decode("utf-8", "surrogateescape").strip()
            sub = posix_rel(rel)
            return loc, (base + sub + "/") if sub else base, ""
    return None, "", ("the hook copy is not a git checkout and no marketplace clone holds the release"
                      + (f" ({problem})" if problem else ""))


def posix_rel(rel: str) -> str:
    """`./plugins/playbook` → `plugins/playbook`; `.` → ``."""
    r = rel.replace("\\", "/").strip("/")
    while r.startswith("./"):
        r = r[2:]
    return "" if r in (".", "") else r


def _scan_installed(root: Path) -> "tuple[dict[str, tuple[str, int | str]], list[str]]":
    """{rel: ("file", size) | ("link", <blob sha of the target text>)} for every entry
    under `root`, without following symlinks and WITHOUT reading any file yet (only
    paths the release also has are hashed later — a planted huge extra file costs
    nothing). Special files (FIFO, socket, device) are listed and never opened: a
    FIFO would block the doctor forever."""
    out: "dict[str, tuple[str, int | str]]" = {}
    special: "list[str]" = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            rel = Path(e.path).relative_to(root).as_posix()
            if _is_runtime_artifact(rel):
                continue
            try:
                if e.is_symlink():
                    out[rel] = ("link", _sha(os.readlink(e.path).encode("utf-8", "surrogateescape")))
                elif e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    out[rel] = ("file", e.stat(follow_symlinks=False).st_size)
                else:
                    special.append(rel)
            except OSError:
                out[rel] = ("file", -1)
    return out, special


_CHUNK = 1 << 20
_NORMALISE_MAX = 16 << 20        # CRLF re-check only for files up to 16 MiB


def _stream_sha(path: Path, size: int) -> str:
    """Git blob sha of a regular file, streamed (the header needs only its size)."""
    h = hashlib.sha1(b"blob %d\0" % size)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _release_attrs(repo: str, sha: str, paths: "list[str]") -> "dict[str, dict[str, str]] | None":
    """`text`/`eol`/`filter` of each path AS OF the release commit (`git check-attr
    --source`), which reads attributes and executes nothing. None when this git
    cannot (no `--source`, git < 2.40) — the caller then forgives nothing."""
    if not paths:
        return {}
    r = _git(repo, "check-attr", f"--source={sha}", "text", "eol", "filter", "--", *paths)
    if r is None or r.returncode != 0:
        return None
    out: "dict[str, dict[str, str]]" = {}
    for line in r.stdout.decode("utf-8", "surrogateescape").splitlines():
        path, _, rest = line.rpartition(": ")
        path, _, attr = path.rpartition(": ")
        out.setdefault(path, {})[attr] = rest
    return out


def content_differences(repo: str, prefix: str, install_path: str, sha: str) -> "list[str]":
    """The installed entries against `<sha>:<prefix>` in `repo`, both ways, blob by
    blob. Nothing is executed but `git ls-tree`/`check-attr`/`config`: a file whose
    raw bytes differ is forgiven ONLY for CRLF line endings, and only where git
    itself would normalise it — the release commit's attributes say text (or leave
    it to `core.autocrlf` true/input) and name no filter. `__pycache__/` and
    `*.pyc` are runtime artifacts."""
    tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha, "--", prefix or ".")
    if tree is None or tree.returncode != 0:
        return [f"could not verify the installed content — release commit {sha[:7]} not in {repo}"]
    release: "dict[str, tuple[str, str]]" = {}
    for rec in tree.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        fields = meta.split()
        rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
        if len(fields) == 3 and fields[1] == "blob" and not _is_runtime_artifact(rel):
            release[rel] = (fields[0], fields[2])
    root = Path(install_path)
    installed, special = _scan_installed(root)
    missing = sorted(set(release) - set(installed))
    extra = sorted(set(installed) - set(release))
    changed, moded, crlf_only = [], [], []
    for rel in sorted(set(release) & set(installed)):
        mode, blob = release[rel]
        kind, val = installed[rel]
        if (kind == "link") != (mode == "120000"):
            changed.append(rel)
            continue
        if kind == "link":
            if val != blob:
                changed.append(rel)
            continue
        try:
            got = _stream_sha(root / rel, int(val))
        except (OSError, ValueError):
            changed.append(rel)
            continue
        if got != blob:
            data = None
            if 0 <= int(val) <= _NORMALISE_MAX:
                try:
                    data = (root / rel).read_bytes()
                except OSError:
                    data = None
            if data is not None and b"\r\n" in data and b"\0" not in data \
                    and _sha(data.replace(b"\r\n", b"\n")) == blob:
                crlf_only.append(rel)          # decided below against the release's attributes
            else:
                changed.append(rel)
            continue
        # the execute bit decides which copy a wrapper selects; Windows has none
        if os.name != "nt" and mode in ("100644", "100755"):
            try:
                x = bool(os.lstat(root / rel).st_mode & 0o111)
            except OSError:
                continue
            if x != (mode == "100755"):
                moded.append(rel)
    if crlf_only:
        attrs = _release_attrs(repo, sha, [prefix + r for r in crlf_only])
        cfg = _git(repo, "config", "--get", "core.autocrlf")
        autocrlf = bool(cfg and cfg.returncode == 0 and cfg.stdout.strip().lower() in (b"true", b"input"))
        for rel in crlf_only:
            a = (attrs or {}).get(prefix + rel, {}) if attrs is not None else None
            ok = a is not None and a.get("filter", "unspecified") == "unspecified" and (
                a.get("text") == "set" or a.get("eol") in ("lf", "crlf")
                or (a.get("text") in ("auto", "unspecified") and autocrlf))
            if not ok:
                changed.append(rel)
    diffs = []
    for label, items in (("missing from the install", missing), ("not in the release", extra),
                         ("differs from the release", sorted(changed)), ("mode differs from the release", moded),
                         ("not a regular file (never opened)", sorted(special))):
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
    """[(tag, text)] — always four `plugin: <label> copy — …` lines (hook, installed,
    launcher, doctor; an undeterminable copy is a WARN naming why), then the
    `plugin: copies agree` verdict: PASS or WARN, never FAIL (a disagreement is a
    state to see, not a broken install)."""
    home = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    doctor_root = Path(doctor_root) if doctor_root is not None else Path(__file__).resolve().parent.parent
    lines: "list[tuple[str, str]]" = []
    diffs: "list[str]" = []

    script = resolve_launcher_script(doctor_root, project, home)
    sel, problem = selected_entry(home, script)

    # the project launcher's state first: when it is custom, stale or missing, the
    # installed/hook lines come from THIS copy's resolver, and must say so
    wrapper = project / ".claude" / "bin" / "tasks"
    try:
        wtext = wrapper.read_text(encoding="utf-8")
    except OSError:
        wtext = None
    if wtext is None:
        wstate = "missing"
    elif "# playbook-managed" not in wtext:
        wstate = "custom"
    elif wtext != wrapper_template(doctor_root, "tasks"):
        wstate = "stale"
    else:
        wstate = "current"
    chosen_by = ("" if wstate == "current" else
                 f" — chosen by this doctor's resolver; the project's launcher is {wstate}")

    hook = None
    if sel is None:
        lines.append(("WARN", f"plugin: hook copy — unknown ({problem})"))
        lines.append(("WARN", f"plugin: installed copy — {problem}"))
        diffs.append(problem)
    else:
        key, entry = sel
        hook, hproblem = hook_copy_path(home, key, entry)
        sha = entry.get("gitCommitSha") or ""
        iv = entry.get("version") or "?"
        if hook is None:
            lines.append(("WARN", f"plugin: hook copy — unknown ({hproblem})"))
            diffs.append(f"hook copy unknown: {hproblem}")
            hv = None
        else:
            hv = manifest_version(hook)
            head, clean = _git_state(hook)
            tail = f" (HEAD {head[:7]}{'' if clean else ', dirty'})" if head else ""
            lines.append(("INFO", f"plugin: hook copy — {hook} v{hv or '?'}{tail}"))
            if head:
                if clean is not True:
                    diffs.append("the hook copy has uncommitted changes")
                if sha and head != sha:
                    diffs.append(f"hook copy HEAD {head[:7]} != installed sha {sha[:7]}")
            if hv != iv:
                diffs.append(f"hook copy manifest v{hv} != installed entry v{iv}")
        lines.append(("INFO", f"plugin: installed copy — {entry['installPath']} v{iv}"
                              + (f" (sha {sha[:7]})" if sha else "") + chosen_by))
        ivm = manifest_version(entry["installPath"])
        if ivm != iv:
            diffs.append(f"installed copy manifest v{ivm} != installed entry v{iv}")
        if sha:
            repo, prefix, rproblem = _release_repo(hook, home, key)
            if repo is None:
                diffs.append(f"could not verify the installed content — {rproblem}")
            else:
                diffs.extend(content_differences(repo, prefix, entry["installPath"], sha))
        else:
            diffs.append("installed entry has no gitCommitSha — content unverified")

    # launcher (read, never executed)
    launcher = None
    if wstate == "missing":
        lines.append(("WARN", f"plugin: launcher copy — none ({wrapper} is missing)"))
        diffs.append("no project launcher")
    elif wstate == "custom":
        lines.append(("WARN", f"plugin: launcher copy — custom launcher — not executed, target unknown ({wrapper})"))
        diffs.append("custom launcher")
    elif wstate == "stale":
        lines.append(("WARN", f"plugin: launcher copy — stale launcher — target unknown, not executed ({wrapper})"))
        diffs.append("stale launcher")
    elif not script:
        lines.append(("WARN", "plugin: launcher copy — unknown (the resolver selected no copy)"))
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
    if known and not any(_same_path(doctor_root, p) for p in known):
        diffs.append("the doctor runs from a fourth copy")

    if diffs:
        lines.append(("WARN", "plugin: copies agree — NO: " + "; ".join(diffs)))
    else:
        lines.append(("PASS", "plugin: copies agree — hook, launcher and installed copies are one release"))
    return lines
