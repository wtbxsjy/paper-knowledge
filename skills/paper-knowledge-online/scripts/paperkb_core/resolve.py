"""Stage 0 — turn what the user gave into one DOI or arXiv id. Pure rules.

Input ``ref`` (any subset)::

    {"id": "<DOI | arXiv id/URL | PMID | PMCID>", "doi", "arxiv", "pmid", "pmcid",
     "title", "authors": "Kong, H; ..." or [...], "journal", "year"}

Output::

    {"status": "resolved", "kind": "doi"|"arxiv", "id": "...", "via": "...", "checks": {...}}
    {"status": "needs_user", "reason": "...", "candidates": [...]}
    {"status": "failed", "reason": "..."}

Title-only auto-acceptance requires ALL of:
  * best candidate title similarity >= ACCEPT (0.95, on folded strings);
  * every field the user supplied (first author, year ±1, journal) matches;
  * no candidate with a *different* identifier reaches AMBIGUOUS (0.90) —
    e.g. a preprint and its journal version with the same title are shown to
    the user instead of one being picked.
Otherwise the candidates and their per-field checks go to the user.
"""
from __future__ import annotations

import difflib
from typing import Optional

from . import compare, ids
from .sources import arxiv, crossref, pmc

ACCEPT = 0.95
AMBIGUOUS = 0.90
SHOW = 0.50
# CrossRef item types that are never the paper itself (supplement files, reviews, grants)
NOT_PAPERS = {"component", "peer-review", "grant", "dataset"}


def title_similarity(a: str, b: str) -> float:
    fa, fb = compare.fold(a), compare.fold(b)
    if not fa or not fb:
        return 0.0
    return difflib.SequenceMatcher(None, fa, fb).ratio()


def _first_author(ref: dict) -> str:
    a = ref.get("authors") or ref.get("first_author") or ""
    if isinstance(a, list):
        return a[0] if a else ""
    return a.split(";")[0].split(" and ")[0].strip()


def _family_forms(name: str) -> set[str]:
    n = (name or "").strip()
    if not n:
        return set()
    toks = [n.split(",")[0]] if "," in n else n.replace(".", " ").split()
    return {compare.fold(t) for t in toks if len(compare.fold(t)) >= 2}


def author_matches(user_name: str, cand_authors: list[str]) -> bool:
    if not cand_authors:
        return False
    forms = _family_forms(user_name)
    for ca in cand_authors[:3]:  # user's "first author" might be listed 2nd/3rd on equal-contribution papers
        fam = compare.fold(ca.split(",")[0]) if "," in ca else compare.fold(ca.split()[-1] if ca.split() else "")
        if fam and fam in forms:
            return True
        if compare.authors_agree(user_name, ca):
            return True
    return False


def _crossref_candidate(item: dict) -> dict:
    authors = [f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in item.get("author") or []]
    issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    years = set()
    for k in ("issued", "published-print", "published-online"):
        dp = ((item.get(k) or {}).get("date-parts") or [[None]])[0]
        if dp and dp[0]:
            years.add(int(dp[0]))
    return {"kind": "doi", "id": (item.get("DOI") or "").lower(),
            "title": (item.get("title") or [""])[0], "authors": authors,
            "year": issued[0] if issued else None,
            "journal": (item.get("container-title") or [""])[0],
            "journal_abbrev": (item.get("short-container-title") or [""])[0],
            "years": sorted(years), "type": item.get("type"), "source": "crossref"}


def _arxiv_candidate(r: dict) -> dict:
    return {"kind": "arxiv", "id": r["arxiv_id"], "title": r["title"], "authors": r["authors"],
            "year": r["year"], "journal": "arXiv", "journal_abbrev": "arXiv",
            "years": [r["year"]] if r.get("year") else [],
            "type": "preprint", "source": "arxiv", "published_doi": r.get("published_doi")}


CONTAIN_MIN = 30   # folded chars; a truncated title this long that is contained counts as a match


def score(ref: dict, cand: dict) -> dict:
    sim = title_similarity(ref.get("title", ""), cand.get("title", ""))
    fu, fc = compare.fold(ref.get("title", "")), compare.fold(cand.get("title", ""))
    contained = len(fu) >= CONTAIN_MIN and fu in fc
    checks = {"title": round(max(sim, ACCEPT) if contained else sim, 3)}
    if contained and sim < ACCEPT:
        checks["title_contained"] = True
    fa = _first_author(ref)
    if fa:
        checks["first_author"] = author_matches(fa, cand.get("authors") or [])
    if ref.get("year"):
        try:
            checks["year"] = cand.get("year") is not None and abs(int(ref["year"]) - int(cand["year"])) <= 1
        except (TypeError, ValueError):
            checks["year"] = False
    if ref.get("journal"):
        nj = compare.norm_venue(ref["journal"])
        checks["journal"] = nj in {compare.norm_venue(cand.get("journal")), compare.norm_venue(cand.get("journal_abbrev"))}
    return checks


def _supplied_ok(checks: dict) -> bool:
    return all(v for k, v in checks.items() if k not in ("title", "title_contained"))


def resolve(ref: dict) -> dict:
    # 1. explicit identifiers win
    for field in ("doi", "arxiv", "pmcid", "pmid", "id"):
        val = (ref.get(field) or "").strip() if isinstance(ref.get(field), str) else ref.get(field)
        if not val:
            continue
        kind, norm = ids.classify(str(val))
        if field == "pmid" and not kind:
            kind, norm = "pmid", ids.normalize_pmid(str(val))
        if kind in ("doi", "arxiv"):
            return {"status": "resolved", "kind": kind, "id": norm, "via": f"given:{field}"}
        if kind in ("pmid", "pmcid") and norm:
            m = pmc.idconv(norm)
            if m and m.get("doi"):
                return {"status": "resolved", "kind": "doi", "id": m["doi"], "via": f"idconv:{kind}",
                        "pmcid": m.get("pmcid"), "pmid": m.get("pmid")}
            return {"status": "needs_user", "reason": f"{kind} {norm} has no DOI in the PMC ID converter",
                    "candidates": []}
        if field != "id":
            return {"status": "failed", "reason": f"unrecognised {field}: {val!r}"}

    title = (ref.get("title") or "").strip()
    if not title:
        return {"status": "failed", "reason": "no identifier and no title"}

    # 2. title search: CrossRef, then arXiv when CrossRef has no confident hit
    query = " ".join(x for x in (title, _first_author(ref), ref.get("journal") or "") if x)
    filters = None
    try:
        y = int(ref["year"]) if ref.get("year") else None
    except (TypeError, ValueError):
        y = None
    if y:
        filters = {"from-pub-date": str(y - 1), "until-pub-date": str(y + 1)}
    cands = [_crossref_candidate(i) for i in crossref.search_bibliographic(query, rows=8, filters=filters)
             if i.get("type") not in NOT_PAPERS][:5]
    scored = [(c, score(ref, c)) for c in cands]
    if not any(s["title"] >= ACCEPT for _, s in scored):
        scored += [(c, score(ref, c)) for c in map(_arxiv_candidate, arxiv.search_title(title, rows=3))]
    scored.sort(key=lambda cs: cs[1]["title"], reverse=True)

    shown = [dict(c, checks=s) for c, s in scored if s["title"] >= SHOW][:6]
    if not scored or scored[0][1]["title"] < SHOW:
        return {"status": "needs_user", "reason": "no candidate with a similar title", "candidates": shown}

    # candidates consistent with everything the user supplied
    good = [(c, s) for c, s in scored if s["title"] >= ACCEPT and _supplied_ok(s)]
    near = [(c, s) for c, s in scored if s["title"] >= AMBIGUOUS and _supplied_ok(s)]
    ids_good = {c["id"] for c, _ in good}
    ids_near = {c["id"] for c, _ in near}
    if len(ids_good) == 1 and ids_near == ids_good:
        c, s = good[0]
        return {"status": "resolved", "kind": c["kind"], "id": c["id"], "via": f"title:{c['source']}", "checks": s}
    if len(ids_good) > 1 and y:
        # tie-break only when exactly one candidate has ANY date (issued/print/online) in the user's year;
        # print-vs-online years make "year == y" alone unsafe (seen: two annual GenBank papers).
        exact = {c["id"]: (c, s) for c, s in good if y in (c.get("years") or [c.get("year")])}
        others_close = any(abs(int(yy) - y) <= 1 for c, _ in good if c["id"] not in exact
                           for yy in (c.get("years") or [c.get("year")]) if yy)
        # CrossRef dates are sometimes incomplete (missing print date), so a neighbour
        # within one year keeps it ambiguous -> ask rather than risk the wrong paper.
        if len(exact) == 1 and not others_close:
            c, s = next(iter(exact.values()))
            return {"status": "resolved", "kind": c["kind"], "id": c["id"],
                    "via": f"title:{c['source']}+year", "checks": s,
                    "note": f"{len(ids_good)} records share the title; only this one has a {y} publication date"}
    best, bs = scored[0]
    if len(ids_good) > 1 or (good and ids_near != ids_good):
        reason = "several records match (e.g. preprint and journal version)"
    elif bs["title"] >= ACCEPT:
        reason = "title matches but a supplied field does not: " + ", ".join(
            k for k, v in bs.items() if k not in ("title", "title_contained") and not v)
    else:
        reason = f"best title similarity {bs['title']:.2f} is below {ACCEPT}"
    return {"status": "needs_user", "reason": reason, "candidates": shown}


def reference_check(ref: dict, record: Optional[dict]) -> Optional[str]:
    """When the user gave a DOI *and* a title, flag an obvious mismatch (typo'd DOI)."""
    if not record or not ref.get("title") or not (ref.get("doi") or ref.get("id") or ref.get("arxiv")):
        return None
    sim = title_similarity(ref["title"], record.get("title", ""))
    if sim < 0.80:
        return (f"the title you gave does not match the record for this identifier "
                f"(similarity {sim:.2f}): {record.get('title', '')!r}")
    return None
