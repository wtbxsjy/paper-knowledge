"""Identifier normalisation and extraction (DOI, PMID, PMCID, arXiv). Pure rules."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("paperkb.ids")

DOI_PATTERN = re.compile(
    r'\b(10\.\d{4,}/[^\s<>"\'\]\[\}\#]+)', re.IGNORECASE
)


def normalize_doi(doi: str) -> str:
    """去前缀 + 转小写。

    服务端本身会归一化（实测三种写法等价），这里做一遍只为让日志与
    比对键稳定，不是接口要求。
    """
    d = (doi or "").strip()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^doi:\s*", "", d, flags=re.IGNORECASE)
    return d.strip().lower()


def extract_doi_from_markdown(md_path: Path) -> Optional[str]:
    """Extract DOI from parsed markdown text."""
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None

    # Search first 5000 chars (front matter is most likely to contain DOI)
    matches = DOI_PATTERN.findall(text[:5000])
    if matches:
        # Clean trailing punctuation
        doi = matches[0].rstrip('.,;:)}]')
        log.info("  📎 Found DOI: %s", doi)
        return doi

    log.info("  ⚠️ No DOI found in markdown front matter")
    return None


def extract_doi_from_text(text: str) -> Optional[str]:
    """Same rule as ``extract_doi_from_markdown`` on an in-memory string."""
    matches = DOI_PATTERN.findall((text or "")[:5000])
    return matches[0].rstrip('.,;:)}]') if matches else None


PMCID_RE = re.compile(r"^(?:pmc)?\s*(\d{3,})$", re.IGNORECASE)
PMID_RE = re.compile(r"^(?:pmid:?\s*)?(\d{1,9})$", re.IGNORECASE)
# new-style 2301.01234(v2) and old-style hep-th/9901001(v1)
ARXIV_RE = re.compile(
    r"^(?:arxiv:\s*)?((?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[A-Z]{2})?/\d{7}))(v\d+)?$",
    re.IGNORECASE)
ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([^\s?#]+?)(?:\.pdf)?(?:[?#].*)?$",
                          re.IGNORECASE)
ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)


def normalize_pmcid(s: str) -> str:
    m = PMCID_RE.match((s or "").strip())
    return f"PMC{m.group(1)}" if m else ""


def normalize_pmid(s: str) -> str:
    m = PMID_RE.match((s or "").strip())
    return m.group(1) if m else ""


def normalize_arxiv(s: str) -> str:
    """Return the bare arXiv id without version, or '' if not an arXiv id."""
    t = (s or "").strip()
    m = ARXIV_URL_RE.search(t)
    if m:
        t = m.group(1)
    d = normalize_doi(t)
    m = ARXIV_DOI_RE.match(d)
    if m:
        t = m.group(1)
    m = ARXIV_RE.match(t)
    return m.group(1) if m else ""


def classify(identifier: str) -> tuple[str, str]:
    """Classify a user-supplied identifier.

    Returns (kind, normalised) with kind in {'doi', 'arxiv', 'pmcid', 'pmid', ''}.
    arXiv DOIs (10.48550/arXiv.*) are reported as 'arxiv'. A bare integer is a
    PMID only when it has no other reading.
    """
    s = (identifier or "").strip()
    if not s:
        return "", ""
    ax = normalize_arxiv(s)
    if ax:
        return "arxiv", ax
    d = normalize_doi(s)
    if d.startswith("10.") and "/" in d:
        return "doi", d
    if s.lower().startswith("pmc"):
        p = normalize_pmcid(s)
        if p:
            return "pmcid", p
    p = normalize_pmid(s)
    if p:
        return "pmid", p
    return "", ""
