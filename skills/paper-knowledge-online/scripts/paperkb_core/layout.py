"""On-disk contract for one paper, identical for online and local skills.

    papers/<key>/
      manifest.json     stage state and provenance            (manifest.py)
      metadata.json     verified record + field provenance + comparison
      review.md         only while a decision is pending (human-readable)
      index.md          full text as Markdown, images referenced as images/...
      images/           figures
      source/           what the full text was built from (paper.pdf, pmc.xml, ...)
      parser/           raw parser output (e.g. MinerU ZIP content), for audit

Downstream consumers (knowledge-base ingest) need only index.md, images/ and
metadata.json. ``fulltext_source`` and ``parser`` in the manifest record how
the text was obtained (e.g. ``pmc-oa-jats`` / ``jats2md``; ``arxiv-pdf`` /
``mineru-cloud``; ``user-pdf`` / ``mineru-4.0.12-standard``).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .manifest import atomic_write_json

IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def write_metadata(paper_dir: Path, record: dict) -> Path:
    p = Path(paper_dir) / "metadata.json"
    atomic_write_json(p, record)
    return p


def install_markdown(paper_dir: Path, md_text: str, images_src: Path | None = None) -> dict:
    """Write index.md (+ images/) atomically enough: images first, then index.md."""
    d = Path(paper_dir)
    img_dst = d / "images"
    if images_src is not None and Path(images_src).is_dir():
        if img_dst.exists():
            shutil.rmtree(img_dst)
        shutil.copytree(images_src, img_dst)
    tmp = d / "index.md.tmp"
    tmp.write_text(md_text, encoding="utf-8")
    tmp.replace(d / "index.md")
    return validate(d)


def validate(paper_dir: Path, min_chars: int = 500) -> dict:
    """Mechanical checks used before a parse stage is marked done."""
    d = Path(paper_dir)
    md = d / "index.md"
    if not md.is_file():
        return {"ok": False, "reason": "index.md missing"}
    return check_markdown(md.read_text(encoding="utf-8", errors="replace"), d, min_chars)


def check_markdown(text: str, base: Path, min_chars: int = 500) -> dict:
    """Same checks on in-memory Markdown whose image paths are relative to ``base``."""
    d = Path(base)
    refs = [r for r in IMG_RE.findall(text) if not r.startswith(("http://", "https://", "data:"))]
    missing = [r for r in refs if not (d / r).is_file()]
    ok = len(text) >= min_chars and not missing
    return {"ok": ok, "chars": len(text), "image_refs": len(refs), "missing_images": missing[:10],
            **({} if ok else {"reason": "too short" if len(text) < min_chars else "unresolved image refs"})}
