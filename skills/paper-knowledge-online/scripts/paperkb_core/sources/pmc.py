"""PMC Open Access Subset via the PMC Cloud Service (public AWS bucket).

NCBI retired the OA Web Service and the FTP article packages in August 2026;
the per-article files now live at::

    https://pmc-oa-opendata.s3.amazonaws.com/<PMCID>.<version>/
        <PMCID>.<v>.json   metadata: is_pmc_openaccess, license_code, is_retracted, ...
        <PMCID>.<v>.xml    JATS full text
        <PMCID>.<v>.pdf
        <PMCID>.<v>.txt
        *.jpg / *.gif ...  figures referenced from the XML

Only items whose JSON says ``is_pmc_openaccess: true`` are used (user decision:
PMC OA subset only). DOI -> PMCID mapping uses NCBI's ID Converter API.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .. import http

log = logging.getLogger("paperkb.pmc")
BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"


def _get(url: str, timeout=30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": http.user_agent()})
    with http.urlopen(req, timeout=timeout) as r:
        return r.read()


def idconv(identifier: str) -> Optional[dict]:
    """Map a DOI / PMID / PMCID to {doi, pmcid, pmid}. None if unknown or on failure."""
    params = {"ids": identifier, "format": "json", "tool": http.TOOL_NAME}
    email = http.contact_email()
    if email:
        params["email"] = email
    try:
        data = json.loads(_get(IDCONV + "?" + urllib.parse.urlencode(params)))
    except Exception as e:
        log.warning("  ⚠️ PMC ID converter failed: %s", e)
        return None
    for r in data.get("records") or []:
        if r.get("status") == "error" or not (r.get("pmcid") or r.get("pmid")):
            continue
        return {"doi": (r.get("doi") or "").lower() or None, "pmcid": r.get("pmcid"),
                "pmid": str(r["pmid"]) if r.get("pmid") else None}
    return None


def latest_version_prefix(pmcid: str) -> Optional[str]:
    """'PMC3531190.1' for the highest version present in the bucket, else None."""
    q = urllib.parse.urlencode({"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"})
    try:
        xml = _get(f"{BUCKET}/?{q}").decode("utf-8", "replace")
    except Exception as e:
        log.warning("  ⚠️ PMC bucket listing failed: %s", e)
        return None
    prefixes = re.findall(rf"<Prefix>({re.escape(pmcid)}\.(\d+))/</Prefix>", xml)
    if not prefixes:
        return None
    return max(prefixes, key=lambda p: int(p[1]))[0]


def list_files(prefix: str) -> list[str]:
    q = urllib.parse.urlencode({"list-type": "2", "prefix": f"{prefix}/", "max-keys": "1000"})
    xml = _get(f"{BUCKET}/?{q}").decode("utf-8", "replace")
    return [k.split("/", 1)[1] for k in re.findall(r"<Key>([^<]+)</Key>", xml) if "/" in k]


def article_metadata(prefix: str) -> Optional[dict]:
    try:
        return json.loads(_get(f"{BUCKET}/{prefix}/{prefix}.json"))
    except Exception as e:
        log.warning("  ⚠️ PMC article metadata failed: %s", e)
        return None


def oa_status(pmcid: str) -> dict:
    """{'in_oa_subset': bool, 'prefix', 'meta', 'reason'} — never raises."""
    prefix = latest_version_prefix(pmcid)
    if not prefix:
        return {"in_oa_subset": False, "reason": "not in PMC cloud bucket", "prefix": None, "meta": None}
    meta = article_metadata(prefix)
    if not meta:
        return {"in_oa_subset": False, "reason": "metadata unavailable", "prefix": prefix, "meta": None}
    if meta.get("is_pmc_openaccess") is not True:
        why = "author manuscript (not OA subset)" if meta.get("is_manuscript") else "not in OA subset"
        return {"in_oa_subset": False, "reason": why, "prefix": prefix, "meta": meta}
    return {"in_oa_subset": True, "reason": "ok", "prefix": prefix, "meta": meta}


def download(prefix: str, dest: Path, include_pdf: bool = True) -> dict:
    """Download XML, JSON, figures (and PDF) of one article into ``dest``."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    files = list_files(prefix)
    got = []
    for name in files:
        if name.endswith(".txt") or (name.endswith(".pdf") and not include_pdf):
            continue
        if "/" in name or name.startswith("."):
            continue  # flat layout expected; never write outside dest
        (dest / name).write_bytes(_get(f"{BUCKET}/{prefix}/{urllib.parse.quote(name)}", timeout=120))
        got.append(name)
    return {"prefix": prefix, "files": got,
            "xml": next((f for f in got if f == f"{prefix}.xml"), None),
            "pdf": next((f for f in got if f == f"{prefix}.pdf"), None)}
