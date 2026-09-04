"""Case model + corpus loader (step 1 ships the empty-corpus skeleton; step 2
adds schema validation of `corpus.json`, `case.json` and `truth.json`)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class CorpusError(ValueError):
    """The corpus on disk violates the schema. Message names the offending case."""


@dataclass
class Case:
    id: str
    path: Path
    kind: str = ""
    area: str = ""
    difficulty: str = ""
    truth_version: int = 1
    meta: dict = field(default_factory=dict)

    def describe(self) -> str:
        return json.dumps(self.meta, indent=2, sort_keys=True)


@dataclass
class Corpus:
    root: Path
    version: int
    cases: list

    def get(self, case_id: str):
        for c in self.cases:
            if c.id == case_id:
                return c
        return None


def load_corpus(root: Path) -> Corpus:
    root = Path(root)
    if not root.is_dir():
        raise CorpusError(f"corpus dir not found: {root}")
    index = root / "corpus.json"
    if not index.exists():
        return Corpus(root=root, version=0, cases=[])      # an empty corpus is valid
    raise CorpusError("corpus.json present but the loader is not implemented yet (step 2)")
