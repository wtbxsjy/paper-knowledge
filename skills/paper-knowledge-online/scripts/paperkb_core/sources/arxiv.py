"""arXiv API (export.arxiv.org): metadata by id, title search, PDF location.

arXiv asks API clients to wait ~3 s between calls; ``_get`` enforces that.
Records use the legacy shape so ``compare`` can treat arXiv as the primary
source for preprints that have no CrossRef DOI (arXiv DOIs are DataCite).
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

from .. import http

log = logging.getLogger("paperkb.arxiv")
API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
MIN_INTERVAL = 3.0
_last = [0.0]


def _get(params: dict) -> Optional[bytes]:
    wait = MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    url = API + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": http.user_agent()})
        with http.urlopen(req, timeout=30) as r:
            return r.read()
    except Exception as e:
        log.warning("  ⚠️ arXiv query failed: %s", e)
        return None
    finally:
        _last[0] = time.time()


def _text(e, path) -> str:
    n = e.find(path, NS)
    return " ".join((n.text or "").split()) if n is not None and n.text else ""


def _entry_to_record(e) -> dict:
    abs_url = _text(e, "a:id")
    aid_v = abs_url.rsplit("/abs/", 1)[-1]
    m = re.match(r"^(.+?)(v\d+)?$", aid_v)
    aid = m.group(1)
    names = [_text(a, "a:name") for a in e.findall("a:author", NS)]
    # arXiv gives "Given Family"; store "Family, Given" like CrossRef/PubMed records
    authors = []
    for n in names:
        parts = n.rsplit(" ", 1)
        authors.append(f"{parts[1]}, {parts[0]}" if len(parts) == 2 else n)
    published = _text(e, "a:published")
    pdf = next((l.get("href") for l in e.findall("a:link", NS) if l.get("title") == "pdf"), "")
    journal_doi = _text(e, "x:doi")
    return {
        "title": _text(e, "a:title"),
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "year": int(published[:4]) if published[:4].isdigit() else None,
        "journal": "arXiv",
        "journal_abbrev": "arXiv",
        "doi": f"10.48550/arxiv.{aid}".lower(),
        "arxiv_id": aid,
        "arxiv_version": m.group(2),
        "journal_ref": _text(e, "x:journal_ref"),
        "published_doi": journal_doi.lower() or None,   # DOI of the journal version, if the authors linked one
        "license": _text(e, "x:license") or None,
        "pdf_url": pdf or f"https://arxiv.org/pdf/{aid_v}",
        "url": f"https://arxiv.org/abs/{aid}",
        "source": "arxiv",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _parse(feed: bytes) -> list[dict]:
    root = ET.fromstring(feed)
    out = []
    for e in root.findall("a:entry", NS):
        if e.find("a:title", NS) is None or _text(e, "a:title") == "Error":
            continue
        out.append(_entry_to_record(e))
    return out


def lookup(arxiv_id: str) -> Optional[dict]:
    feed = _get({"id_list": arxiv_id, "max_results": "1"})
    if not feed:
        return None
    recs = _parse(feed)
    return recs[0] if recs else None


def search_title(title: str, rows: int = 5) -> list[dict]:
    # phrase queries break on punctuation; AND the longest distinctive words instead
    words = sorted({w for w in re.findall(r"[A-Za-z0-9]+", title) if len(w) > 3}, key=len, reverse=True)[:8]
    if not words:
        return []
    feed = _get({"search_query": " AND ".join(f"ti:{w}" for w in words), "max_results": str(rows)})
    return _parse(feed) if feed else []
