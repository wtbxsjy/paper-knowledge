"""Field-by-field comparison of the primary record with Sciverse. Pure rules.

Decision rule (user, 2026-10-25): when the two sources disagree on a field,
list the evidence and **stop for the user** — never pick a side silently.

Primary record = CrossRef (DOI registration agency) or, for arXiv preprints
without a CrossRef DOI, the arXiv API record. Both use the legacy record shape
(``title``, ``first_author``, ``year``, ``journal``, ``journal_abbrev``...).

Normalisation is what keeps the stop rate sane. On the 87-paper baseline the
raw strings differed on *every* paper because CrossRef writes authors as
``Family, Given`` and Sciverse as ``Given Family``; after the rules below only
one genuine disagreement remained (a print-vs-online year).

Per-field outcome:
  agree        both present and equal after normalisation
  fill         only one side has a value — no conflict, value is taken from
               the side that has it (primary preferred) and the origin recorded
  unreliable   Sciverse value present but flagged untrustworthy (scraping noise,
               preprint server venue) — ignored, recorded as evidence
  conflict     both present, different after normalisation -> needs the user
"""
from __future__ import annotations

import html
import re
import unicodedata
from typing import Any, Optional

FIELDS = ("title", "first_author", "year", "journal")

# Sciverse venue strings sometimes carry the citation tail:
#   "Nature Communications, 2021, Vol 12, Issue 1, p1"
VENUE_TAIL_RE = re.compile(r",\s*(?:\d{4}|vol\b|volume\b|issue\b|p\d).*$", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def fold(s: Any) -> str:
    """HTML-unescape, drop tags, strip accents, casefold, keep [a-z0-9] only."""
    t = html.unescape(TAG_RE.sub("", str(s or "")))
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", t.casefold())


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == 0


def norm_title(s: Any) -> str:
    return fold(s)


def norm_venue(s: Any) -> str:
    return fold(VENUE_TAIL_RE.sub("", html.unescape(str(s or ""))))


def name_forms(name: str) -> set[str]:
    """Comparable forms of one personal name, independent of order/format.

    "Fazly Bazzaz, Bibi Sedigheh" -> {"fazlybazzazbibisedigheh", "bibisedighehfazlybazzaz", "fazlybazzaz"}
    "Bibi Sedigheh Fazly Bazzaz"  -> {"bibisedighehfazlybazzaz"}
    """
    n = html.unescape(str(name or "")).strip()
    if not n:
        return set()
    if "," in n:
        family, given = [x.strip() for x in n.split(",", 1)]
        return {fold(family + given), fold(given + " " + family), fold(family)} - {""}
    return {fold(n)} - {""}


def authors_agree(a: str, b: str) -> bool:
    fa, fb = name_forms(a), name_forms(b)
    if not fa or not fb:
        return False
    if fa & fb:
        return True
    # "Family, Given" on one side vs "Given Family" on the other: family-name suffix match
    for x in fa:
        for y in fb:
            if len(x) >= 3 and len(y) >= 3 and (y.endswith(x) or x.endswith(y)):
                return True
    return False


def compare(primary: Optional[dict], sciverse: Optional[dict],
            primary_extra: Optional[dict] = None) -> dict:
    """Compare and classify. Never mutates inputs.

    ``primary_extra`` may carry ``dates`` (from ``crossref.work_dates``) so a
    year conflict can show every CrossRef date as evidence.
    """
    res: dict = {"status": "agree", "conflicts": [], "fields": {},
                 "primary_source": (primary or {}).get("source"),
                 "secondary_source": "sciverse" if sciverse else None}
    if not primary and not sciverse:
        res["status"] = "no_source"
        return res
    if not primary or not sciverse:
        res["status"] = "single_source"
        only = primary or sciverse
        for f in FIELDS:
            v = only.get("venue" if (f == "journal" and only is sciverse) else f)
            if f == "first_author" and only is sciverse:
                v = (sciverse.get("authors") or [""])[0]
            res["fields"][f] = {"outcome": "fill" if not _blank(v) else "missing",
                                "value": v, "from": only.get("source")}
        return res

    s_first = (sciverse.get("authors") or [""])[0]
    pairs = {
        "title": (primary.get("title"), sciverse.get("title")),
        "first_author": (primary.get("first_author"), s_first),
        "year": (primary.get("year"), sciverse.get("year")),
        "journal": (primary.get("journal"), sciverse.get("venue")),
    }
    for f, (a, b) in pairs.items():
        ent: dict = {"primary": a, "sciverse": b}
        if f == "journal" and not _blank(b) and sciverse.get("venue_trustworthy") is False:
            ent.update(outcome="unreliable" if not _blank(a) else "missing",
                       value=a, note=f"sciverse venue rejected: {sciverse.get('venue_reject_reason')}")
        elif _blank(a) and _blank(b):
            ent.update(outcome="missing", value=None)
        elif _blank(a) or _blank(b):
            ent.update(outcome="fill", value=b if _blank(a) else a,
                       **{"from": "sciverse" if _blank(a) else primary.get("source")})
        else:
            if f == "title":
                same = norm_title(a) == norm_title(b)
            elif f == "first_author":
                same = authors_agree(a, b)
            elif f == "year":
                try:
                    same = int(a) == int(b)
                except (TypeError, ValueError):
                    same = False
            else:  # journal: full name or the primary's abbreviation may match
                nb = norm_venue(b)
                same = nb in {norm_venue(a), norm_venue(primary.get("journal_abbrev"))}
            ent.update(outcome="agree" if same else "conflict", value=a if same else None)
            if not same and f == "year" and primary_extra and primary_extra.get("dates"):
                dates = primary_extra["dates"]
                ent["primary_dates"] = dates
                hits = [k for k, v in dates.items() if v and str(v[0]) == str(b)]
                if hits:
                    ent["note"] = f"sciverse year equals CrossRef {', '.join(hits)} year"
        res["fields"][f] = ent
        if ent["outcome"] == "conflict":
            res["conflicts"].append(f)
    if res["conflicts"]:
        res["status"] = "needs_review"
    return res


def resolve(primary: Optional[dict], sciverse: Optional[dict], comparison: dict,
            decisions: Optional[dict] = None) -> dict:
    """Build the final record. Raises if a conflict has no user decision.

    ``decisions``: {field: {"choice": "primary"|"sciverse"|"custom", "value": ...}}
    """
    decisions = decisions or {}
    missing = [f for f in comparison.get("conflicts", []) if f not in decisions]
    if missing:
        raise ValueError(f"unresolved conflicts: {missing}")
    base = dict(primary or {})
    if not primary and sciverse:  # single source = sciverse
        base = {"title": sciverse.get("title"), "authors": sciverse.get("authors") or [],
                "first_author": (sciverse.get("authors") or [""])[0], "year": sciverse.get("year"),
                "journal": sciverse.get("venue") if sciverse.get("venue_trustworthy") else "",
                "doi": sciverse.get("doi"), "source": "sciverse"}
    provenance = {}
    for f, ent in comparison.get("fields", {}).items():
        if ent["outcome"] == "conflict":
            d = decisions[f]
            val = {"primary": ent["primary"], "sciverse": ent["sciverse"]}.get(d["choice"], d.get("value"))
            base[f] = val
            provenance[f] = f"user:{d['choice']}"
        elif ent["outcome"] == "fill" and _blank(base.get(f)):
            base[f] = ent["value"]
            provenance[f] = ent.get("from")
        else:
            provenance[f] = base.get("source")
    base["field_provenance"] = provenance
    return base
