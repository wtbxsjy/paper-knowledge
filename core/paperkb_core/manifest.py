"""Per-paper stage state, so any run can stop and resume.

Run directory layout (shared by the online and local skills)::

    <out>/
      queue.json              inputs and their resolution state
      NEEDS_ATTENTION.md      regenerated each run: what waits on the user
      inbox/                  user drops manually downloaded PDFs here
      papers/<key>/           one directory per resolved paper (see layout.py)
        manifest.json

``key`` is derived from the identifier (stable, known as soon as a DOI or
arXiv id is resolved): ``doi-10.1038_s41598-023-46312-8``, ``arxiv-2301.01234``.

Stage statuses: pending | done | needs_user | failed | skipped.
A stage marked ``needs_user`` carries ``detail.kind`` in
{"choose_candidate", "resolve_conflict", "manual_download"}.
Writes are atomic (tmp file + os.replace).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

STAGES = ("resolve", "verify", "fetch", "parse")
STATUSES = ("pending", "done", "needs_user", "failed", "skipped")
SCHEMA = 1


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def paper_key(kind: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip().lower()).strip("_")
    return f"{kind}-{safe}"[:150]


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Manifest:
    def __init__(self, paper_dir: Path, data: dict):
        self.dir = Path(paper_dir)
        self.data = data

    @property
    def path(self) -> Path:
        return self.dir / "manifest.json"

    @classmethod
    def load_or_create(cls, paper_dir: Path, key: str, reference: dict) -> "Manifest":
        p = Path(paper_dir) / "manifest.json"
        if p.is_file():
            m = cls(paper_dir, json.loads(p.read_text(encoding="utf-8")))
            # remember every way the user has referred to this paper
            refs = m.data.setdefault("references", [])
            if reference and reference not in refs:
                refs.append(reference)
                m.save()
            return m
        m = cls(paper_dir, {
            "schema": SCHEMA, "key": key, "created": now(),
            "references": [reference] if reference else [],
            "stages": {s: {"status": "pending"} for s in STAGES},
            "decisions": {}, "fulltext_source": None, "parser": None,
        })
        m.save()
        return m

    def save(self) -> None:
        self.data["updated"] = now()
        atomic_write_json(self.path, self.data)

    def status(self, stage: str) -> str:
        return self.data["stages"][stage]["status"]

    def set(self, stage: str, status: str, **detail) -> None:
        assert stage in STAGES and status in STATUSES, (stage, status)
        self.data["stages"][stage] = {"status": status, "at": now(), **({"detail": detail} if detail else {})}
        self.save()

    def detail(self, stage: str) -> dict:
        return self.data["stages"][stage].get("detail", {})

    def first_open_stage(self) -> Optional[str]:
        for s in STAGES:
            if self.status(s) not in ("done", "skipped"):
                return s
        return None


def iter_manifests(out_dir: Path):
    for p in sorted((Path(out_dir) / "papers").glob("*/manifest.json")):
        yield Manifest(p.parent, json.loads(p.read_text(encoding="utf-8")))
