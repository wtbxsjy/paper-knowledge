"""CrossRef — the DOI registration agency; primary source for bibliographic fields."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .. import http

log = logging.getLogger("paperkb.crossref")


def verify_by_doi(doi: str) -> Optional[dict]:
    """Query CrossRef API for verified metadata by DOI.

    Returns dict with: title, authors, year, journal, doi, publisher, type, abstract, url
    """
    msg = fetch_message(doi)
    if not msg:
        return None
    return record_from_message(msg, doi)


def fetch_message(doi: str) -> Optional[dict]:
    """GET /works/{doi} and return the raw ``message`` (None on any failure)."""
    url = f"https://api.crossref.org/works/{urllib.request.quote(doi, safe='')}"
    log.info("  🌐 Querying CrossRef: %s", doi)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": http.user_agent()})
        with http.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.warning("  ⚠️ CrossRef HTTP %d for DOI %s", e.code, doi)
        return None
    except Exception as e:
        log.warning("  ⚠️ CrossRef query failed: %s", e)
        return None

    return data.get("message", {}) or None


def record_from_message(msg: dict, doi: str) -> dict:
    """Legacy-shaped record (identical to the original ``verify_by_doi``)."""

    # Extract fields
    title_list = msg.get("title", [])
    title = title_list[0] if title_list else ""

    authors = []
    for a in msg.get("author", []):
        family = a.get("family", "")
        given = a.get("given", "")
        authors.append(f"{family}, {given}" if family else given)

    # Year from multiple possible locations
    issued = msg.get("issued", {})
    date_parts = issued.get("date-parts", [[None]])[0]
    year = date_parts[0] if date_parts else None
    if not year:
        created = msg.get("created", {})
        date_parts = created.get("date-parts", [[None]])[0]
        year = date_parts[0] if date_parts else None

    # Journal / container
    container = msg.get("container-title", [])
    journal = container[0] if container else ""
    short_container = msg.get("short-container-title", [])
    journal_abbrev = short_container[0] if short_container else journal

    # Abstract
    abstract = msg.get("abstract", "")

    # Publisher
    publisher = msg.get("publisher", "")

    # Type
    pub_type = msg.get("type", "")

    # URL
    url_link = msg.get("URL", "")
    if not url_link:
        url_link = f"https://doi.org/{doi}"

    result = {
        "title": title,
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "year": year,
        "journal": journal,
        "journal_abbrev": journal_abbrev,
        "doi": doi,
        "publisher": publisher,
        "type": pub_type,
        "abstract": abstract[:500] if abstract else "",
        "url": url_link,
        "source": "crossref",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    log.info("  ✅ Verified: %s (%d) — %s", result["first_author"], result["year"], journal_abbrev)
    return result


def _ymd(part: dict) -> Optional[list]:
    dp = (part or {}).get("date-parts") or [[None]]
    return [x for x in dp[0] if x is not None] or None


def work_dates(msg: dict) -> dict:
    """All CrossRef date fields that can explain a year disagreement.

    ``issued`` is what ``record_from_message`` uses for ``year``; it is the
    earlier of print/online for most publishers but not all.
    """
    return {k: _ymd(msg.get(k)) for k in
            ("issued", "published-print", "published-online", "published", "created")
            if msg.get(k)}


def search_bibliographic(query: str, rows: int = 5, filters: Optional[dict] = None) -> list[dict]:
    """``/works?query.bibliographic=`` — candidates for title → DOI resolution.

    Returns raw CrossRef items (each with ``DOI``, ``title``, ``author``,
    ``issued``, ``container-title``, ``score``). Empty list on failure.
    """
    params = {"query.bibliographic": query, "rows": str(rows),
              "select": "DOI,title,author,issued,published-print,published-online,container-title,short-container-title,type,score"}
    if filters:
        params["filter"] = ",".join(f"{k}:{v}" for k, v in filters.items())
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": http.user_agent()})
        with http.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning("  ⚠️ CrossRef search failed: %s", e)
        return []
    return (data.get("message") or {}).get("items") or []
