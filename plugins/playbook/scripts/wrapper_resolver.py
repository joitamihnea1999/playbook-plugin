import glob, json, os, sys

def vkey(v):
    # "1.3.10" -> (1,3,10); non-numeric segments (e.g. "unknown") -> -1 so any
    # numbered version outranks them.
    return tuple(int(x) if x.isdigit() else -1 for x in str(v).split("."))

def same_dir(a, b):
    # inode compare survives case-insensitive filesystems (default APFS) where
    # realpath string equality can miss; realpath as fallback for missing paths.
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)

try:
    project = sys.argv[1] if len(sys.argv) > 1 else ""
    # Derive the plugins root from $HOME, matching the bash last-resort
    # `find ~/.claude/plugins` below (bash `~` is $HOME). os.path.expanduser('~')
    # prefers USERPROFILE over $HOME on Windows, so it could look in a different
    # home than the shell that runs the wrapper — the resolver and its own
    # fallback would then disagree. Fall back to expanduser only if $HOME is
    # unset. No-op on POSIX and on Windows where $HOME == USERPROFILE.
    home = os.environ.get("HOME") or os.path.expanduser("~")
    root = os.path.join(home, ".claude", "plugins")
    cands = []  # (rank, version_key, last_updated, path)
    try:
        with open(os.path.join(root, "installed_plugins.json")) as fh:
            data = json.load(fh)
        for key, installs in (data.get("plugins") or {}).items():
            if key.split("@")[0] != "playbook":
                continue
            for e in installs or []:
                ip = e.get("installPath") or ""
                s = ip + "/scripts/WRAPPER_NAME"
                if not (ip and os.access(s, os.X_OK)):
                    continue
                pp = e.get("projectPath") or ""
                if pp:
                    # Pinned to a project: only eligible for THAT project.
                    if not (project and same_dir(pp, project)):
                        continue
                    rank = 0
                else:
                    rank = 1
                cands.append((rank, vkey(e.get("version")), e.get("lastUpdated") or "", s))
    except Exception:
        pass
    if not cands:
        # No usable manifest entry: scan known layouts. Versioned cache dirs
        # (the layout hooks run from) outrank marketplace clone tips.
        for s in glob.glob(os.path.join(root, "cache", "*", "playbook", "*") + "/scripts/WRAPPER_NAME"):
            if os.access(s, os.X_OK):
                ver = os.path.basename(os.path.dirname(os.path.dirname(s)))
                cands.append((0, vkey(ver), "", s))
        if not cands:
            for pat in ("marketplaces/*/plugins/playbook", "cache/*/playbook"):
                for s in glob.glob(os.path.join(root, pat) + "/scripts/WRAPPER_NAME"):
                    if os.access(s, os.X_OK):
                        cands.append((1, (), "", s))
    if cands:
        # Multi-pass stable sort, least-significant key first. Final order:
        # rank asc, version desc, lastUpdated desc, path asc.
        cands.sort(key=lambda t: t[3])
        cands.sort(key=lambda t: t[2], reverse=True)
        cands.sort(key=lambda t: t[1], reverse=True)
        cands.sort(key=lambda t: t[0])
        print(cands[0][3])
except Exception:
    pass
