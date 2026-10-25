"""PubMed E-utilities lookup by free-text query (used as a fallback / cross-check)."""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

from .. import http

log = logging.getLogger("paperkb.pubmed")
TOOL_NAME = http.TOOL_NAME


def verify_by_pubmed(query: str) -> Optional[dict]:
    """Fallback: query PubMed E-utilities by title/author.

    Uses esearch → efetch to get metadata.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    # NCBI 规范要求以查询参数标识调用方；email 仅在用户显式设置时附带。
    CONTACT_EMAIL = http.contact_email()
    ident = f"&tool={TOOL_NAME}" + (f"&email={urllib.request.quote(CONTACT_EMAIL)}" if CONTACT_EMAIL else "")

    # Step 1: Search
    search_url = f"{base}/esearch.fcgi?db=pubmed&retmax=1&retmode=json&term={urllib.request.quote(query)}{ident}"
    log.info("  🌐 Querying PubMed: %s...", query[:60])

    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": http.user_agent()})
        with http.urlopen(req, timeout=10) as resp:
            search_data = json.loads(resp.read())
    except Exception as e:
        log.warning("  ⚠️ PubMed search failed: %s", e)
        return None

    id_list = search_data.get("esearchresult", {}).get("idlist", [])
    if not id_list:
        log.info("  ⚠️ No PubMed results for query")
        return None

    pmid = id_list[0]

    # Step 2: Fetch
    fetch_url = f"{base}/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml{ident}"
    try:
        req = urllib.request.Request(fetch_url, headers={"User-Agent": http.user_agent()})
        with http.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except Exception as e:
        log.warning("  ⚠️ PubMed fetch failed: %s", e)
        return None

    root = ET.fromstring(xml_data)

    # Parse XML
    article = root.find(".//Article")
    if article is None:
        return None

    title_elem = article.find(".//ArticleTitle")
    title = title_elem.text if title_elem is not None else ""

    authors = []
    for author_elem in article.findall(".//Author"):
        last = author_elem.find("LastName")
        fore = author_elem.find("ForeName")
        if last is not None:
            name = last.text
            if fore is not None:
                name = f"{last.text}, {fore.text}"
            authors.append(name)

    year_elem = article.find(".//PubDate/Year")
    year = int(year_elem.text) if year_elem is not None and year_elem.text else None

    journal_elem = article.find(".//Journal/Title")
    journal = journal_elem.text if journal_elem is not None else ""

    doi_elem = article.find(".//ELocationID[@EIdType='doi']")
    doi = doi_elem.text if doi_elem is not None else ""

    abstract_elem = article.find(".//Abstract/AbstractText")
    abstract = abstract_elem.text if abstract_elem is not None else ""

    result = {
        "title": title,
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "year": year,
        "journal": journal,
        "journal_abbrev": journal,
        "doi": doi,
        "pmid": pmid,
        "abstract": abstract[:500] if abstract else "",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "source": "pubmed",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    log.info("  ✅ PubMed verified: %s (%d) — PMID: %s", result["first_author"], result["year"], pmid)
    return result
