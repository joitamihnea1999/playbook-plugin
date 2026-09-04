"""`bench.lib` — library half of the dev-only judge benchmark harness.

Importing this package puts the canonical plugin tree (`plugins/playbook/`) on
`sys.path` so the harness can reuse production helpers as the packages they
are (`tasks.review._judge_status`, `provider.sandbox.resolve_judge_spec`, …)
exactly like the test suite does. The bench never imports the rsync mirror
under `plugins/playbook/scripts/lib/`.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent.parent          # bench/
REPO_ROOT = BENCH_ROOT.parent                                 # playbook-plugin/
PLUGIN_ROOT = REPO_ROOT / "plugins" / "playbook"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

DEFAULT_CORPUS_DIR = BENCH_ROOT / "corpus"
DEFAULT_RUNS_DIR = BENCH_ROOT / "runs"
