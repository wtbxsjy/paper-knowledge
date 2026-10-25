"""Four-stage pipeline: resolve -> verify -> fetch -> parse. Resumable, rule-based.

Every stage is idempotent: re-running ``Run.run()`` skips finished stages and
continues from the first open one. A stage that needs the user records why in
the manifest, writes a human-readable ``review.md`` (or an entry in
``NEEDS_ATTENTION.md``) and the run moves on to other papers.

User decisions are applied with ``choose_candidate`` (stage 0),
``decide`` (stage 1) and ``attach_pdf`` (stage 2), then ``run()`` again.

Exit codes for CLIs (``Run.exit_code()``):
  0  everything done
  2  a decision is needed (candidate choice or metadata conflict)
  3  only manual downloads are pending
  1  a stage failed
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from . import compare, credentials, http, ids, jats, layout, manifest, resolve
from .manifest import Manifest, atomic_write_json, now, paper_key
from .sources import arxiv, crossref, pmc
from .sources import sciverse as sv

log = logging.getLogger("paperkb.pipeline")

EXIT_OK, EXIT_ERROR, EXIT_DECISION, EXIT_DOWNLOAD = 0, 1, 2, 3
CLOUD_INTERVAL_S = 5      # MinerU cloud: strictly serial, fixed gap between uploads
UPLOAD_NOTICE = ("NOTE: PDFs that are not available as PMC OA XML are uploaded to MinerU's cloud "
                 "(mineru.net, servers in China) for parsing. Use --no-upload to stop before any upload.")


def ref_id(ref: dict) -> str:
    return hashlib.sha1(json.dumps(ref, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


# ───────────────────────── input parsing ─────────────────────────

def parse_reference_line(line: str) -> Optional[dict]:
    """One reference per line: an identifier, or a title, or
    ``title | first author | journal | year`` (any trailing fields optional)."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "|" not in s:
        kind, _ = ids.classify(s)
        return {"id": s} if kind else {"title": s}
    parts = [p.strip() for p in s.split("|")]
    kind, _ = ids.classify(parts[0])
    if kind:   # "identifier | title | ..." — the title is used to catch a mistyped identifier
        ref = {"id": parts[0]}
        parts = parts[1:]
        if parts and parts[0]:
            ref["title"] = parts[0]
    else:
        ref = {"title": parts[0]}
    for k, v in zip(("authors", "journal", "year"), parts[1:]):
        if v:
            ref[k] = int(v) if k == "year" and v.isdigit() else v
    return ref


def read_references(path: Path) -> list[dict]:
    p = Path(path)
    if p.suffix.lower() == ".jsonl":
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if p.suffix.lower() == ".csv":
        import csv
        rows = []
        with p.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                ref = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k and (v or "").strip()}
                if "year" in ref and ref["year"].isdigit():
                    ref["year"] = int(ref["year"])
                if ref:
                    rows.append(ref)
        return rows
    return [r for r in map(parse_reference_line, p.read_text(encoding="utf-8").splitlines()) if r]


# ───────────────────────── run ─────────────────────────

class Run:
    def __init__(self, out_dir: Path, *, allow_upload: bool = True, parse_pdf: Optional[Callable] = None,
                 log_fn: Callable[[str], None] = print):
        self.out = Path(out_dir)
        self.papers = self.out / "papers"
        self.inbox = self.out / "inbox"
        self.review_dir = self.out / "review"
        for d in (self.papers, self.inbox, self.review_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.out / "queue.json"
        self.queue = json.loads(self.queue_path.read_text(encoding="utf-8")) if self.queue_path.is_file() else {"items": {}}
        self.allow_upload = allow_upload
        self.parse_pdf = parse_pdf          # injectable for tests / local engine
        self.say = log_fn
        self._last_upload = 0.0
        self._notice_shown = False

    # ---- queue ----
    def save_queue(self) -> None:
        atomic_write_json(self.queue_path, self.queue)

    def add(self, refs: list[dict]) -> list[str]:
        added = []
        for ref in refs:
            rid = ref_id(ref)
            if rid not in self.queue["items"]:
                self.queue["items"][rid] = {"ref": ref, "status": "pending", "added": now()}
                added.append(rid)
        self.save_queue()
        return added

    # ---- stage 0 ----
    def stage_resolve(self, rid: str) -> Optional[Manifest]:
        it = self.queue["items"][rid]
        if it["status"] == "resolved":
            return Manifest.load_or_create(self.papers / it["key"], it["key"], it["ref"])
        if it["status"] == "needs_user" and not it.get("choice"):
            return None
        if it.get("choice"):
            kind, val = it["choice"]["kind"], it["choice"]["id"]
            res = {"status": "resolved", "kind": kind, "id": val, "via": "user:choose"}
        else:
            res = resolve.resolve(it["ref"])
        if res["status"] == "resolved":
            key = paper_key(res["kind"], res["id"])
            it.update(status="resolved", key=key, kind=res["kind"], id=res["id"])
            it.pop("candidates", None)
            m = Manifest.load_or_create(self.papers / key, key, it["ref"])
            m.data.update(kind=res["kind"], id=res["id"])
            for k in ("pmcid", "pmid"):
                if res.get(k):
                    m.data[k] = res[k]
            m.set("resolve", "done", via=res["via"], **({"checks": res["checks"]} if res.get("checks") else {}),
                  **({"note": res["note"]} if res.get("note") else {}))
            self.save_queue()
            return m
        it.update(status=res["status"], reason=res.get("reason"), candidates=res.get("candidates", []))
        self.save_queue()
        self._write_candidate_review(rid, it)
        return None

    def _write_candidate_review(self, rid: str, it: dict) -> None:
        lines = [f"# Which paper did you mean? (`{rid}`)", "",
                 f"Your input: `{json.dumps(it['ref'], ensure_ascii=False)}`", "",
                 f"Why I stopped: {it.get('reason')}", ""]
        if it.get("candidates"):
            lines += ["| # | identifier | year(s) | title | first author | journal | checks |",
                      "|---|---|---|---|---|---|---|"]
            for n, c in enumerate(it["candidates"], 1):
                chk = ", ".join(f"{k}={'✓' if v is True else '✗' if v is False else v}" for k, v in c["checks"].items())
                lines.append(f"| {n} | `{c['id']}` ({c['source']}, {c.get('type') or ''}) | "
                             f"{'/'.join(map(str, c.get('years') or [c.get('year')]))} | {c['title'][:120]} | "
                             f"{(c.get('authors') or [''])[0]} | {c.get('journal') or ''} | {chk} |")
        lines += ["", "Reply with `choose " + rid + " <#>` or `choose " + rid + " <DOI or arXiv id>`, "
                  "or `skip " + rid + "`."]
        (self.review_dir / f"resolve-{rid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def choose_candidate(self, rid: str, choice: str) -> dict:
        it = self.queue["items"][rid]
        if choice.isdigit() and it.get("candidates"):
            c = it["candidates"][int(choice) - 1]
            it["choice"] = {"kind": c["kind"], "id": c["id"]}
        else:
            kind, val = ids.classify(choice)
            if kind not in ("doi", "arxiv"):
                raise ValueError(f"not a DOI or arXiv id: {choice!r}")
            it["choice"] = {"kind": kind, "id": val}
        it["status"] = "pending"
        self.save_queue()
        (self.review_dir / f"resolve-{rid}.md").unlink(missing_ok=True)
        return it["choice"]

    def skip(self, rid_or_key: str) -> list[str]:
        """Skip by reference id (from ``add``) or by paper key (``doi-…`` / ``arxiv-…``)."""
        hit = [r for r, it in self.queue["items"].items() if r == rid_or_key or it.get("key") == rid_or_key]
        if not hit:
            raise KeyError(f"unknown reference id or paper key {rid_or_key!r}")
        for r in hit:
            self.queue["items"][r]["status"] = "skipped"
            (self.review_dir / f"resolve-{r}.md").unlink(missing_ok=True)
        self.save_queue()
        pdir = self.papers / rid_or_key
        if (pdir / "manifest.json").is_file():
            m = self._manifest(rid_or_key)
            for st in manifest.STAGES:
                if m.status(st) != "done":
                    m.set(st, "skipped", reason="skipped by user")
            (pdir / "review.md").unlink(missing_ok=True)
        return hit

    # ---- stage 1 ----
    def stage_verify(self, m: Manifest) -> None:
        if m.status("verify") == "done":
            return
        kind, ident = m.data["kind"], m.data["id"]
        cache = m.dir / "sources.json"
        src = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
        if not src:
            if kind == "arxiv":
                primary = arxiv.lookup(ident)
                extra = {}
                doi = f"10.48550/arxiv.{ident}"
            else:
                msg = crossref.fetch_message(ident)
                primary = crossref.record_from_message(msg, ident) if msg else None
                extra = {"dates": crossref.work_dates(msg)} if msg else {}
                if msg:
                    extra["links"] = [l for l in (msg.get("link") or []) if l.get("URL")]
                    extra["license"] = [l.get("URL") for l in (msg.get("license") or []) if l.get("URL")]
                    extra["relation"] = msg.get("relation") or {}
                    extra["update_to"] = msg.get("update-to") or []
                doi = ident
            secondary = sv.lookup_by_doi(doi)
            src = {"primary": primary, "primary_extra": extra, "sciverse": secondary,
                   "sciverse_state": {"disabled": sv.STATE.disabled, "reason": sv.STATE.reason},
                   "fetched": now()}
            atomic_write_json(cache, src)
        primary, secondary, extra = src["primary"], src["sciverse"], src.get("primary_extra") or {}
        if primary is None and secondary is None:
            m.set("verify", "failed", reason=f"neither {'arXiv' if kind == 'arxiv' else 'CrossRef'} nor Sciverse "
                  f"knows {ident}")
            return
        # user-supplied title vs record: catches mistyped identifiers
        mismatches = [w for w in (resolve.reference_check(r, primary or {"title": (secondary or {}).get("title")})
                                  for r in m.data.get("references", [])) if w]
        if mismatches and "reference" not in m.data["decisions"]:
            self._write_conflict_review(m, None, primary, secondary, extra, mismatches)
            m.set("verify", "needs_user", kind="reference_mismatch", messages=mismatches)
            return
        comp = compare.compare(primary, secondary, extra)
        if comp["status"] == "needs_review" and not all(f in m.data["decisions"] for f in comp["conflicts"]):
            self._write_conflict_review(m, comp, primary, secondary, extra, [])
            m.set("verify", "needs_user", kind="resolve_conflict", fields=comp["conflicts"])
            return
        record = compare.resolve(primary, secondary, comp, m.data["decisions"])
        record = self._enrich(record, secondary)
        record["paperkb"] = {"key": m.data["key"], "kind": kind, "id": ident,
                             "comparison_status": comp["status"],
                             "sciverse": "used" if secondary else f"unavailable ({src['sciverse_state']['reason'] or 'not found'})"}
        if extra.get("update_to") or any(k in (extra.get("relation") or {}) for k in ("is-retracted-by", "has-retraction")):
            record["paperkb"]["warning"] = "CrossRef lists an update/retraction relation for this DOI — check before use"
        layout.write_metadata(m.dir, {"record": record, "comparison": comp})
        (m.dir / "review.md").unlink(missing_ok=True)
        m.set("verify", "done", comparison=comp["status"], decisions=sorted(m.data["decisions"]))

    @staticmethod
    def _enrich(record: dict, svrec: Optional[dict]) -> dict:
        """Sciverse fill-only enrichment (same contract as sciverse.enrich_verified, no extra request)."""
        if not svrec:
            return record
        out = dict(record)
        filled = []
        for f in sv.ENRICH_FIELDS:
            v = svrec.get(f)
            if not sv._is_blank(v) and sv._is_blank(out.get(f)):
                out[f] = v
                filled.append(f)
        out["sciverse_filled"] = filled
        return out

    def _write_conflict_review(self, m, comp, primary, secondary, extra, mismatches) -> None:
        key = m.data["key"]
        p_name = (primary or {}).get("source", "primary")
        L = [f"# Metadata needs your decision — `{key}`", ""]
        if mismatches:
            L += ["## The identifier and the title you gave do not match", ""] + [f"- {x}" for x in mismatches]
            L += ["", f"If the identifier is right: `decide {key} reference=ok`. "
                  f"If not: `skip {key}`, then add the paper again with the correct identifier.", ""]
        if comp:
            L += ["## Fields where the two sources disagree", "",
                  f"| field | {p_name} | sciverse | notes |", "|---|---|---|---|"]
            for f in comp["conflicts"]:
                e = comp["fields"][f]
                L.append(f"| {f} | {e['primary']} | {e['sciverse']} | {e.get('note', '')} |")
            dates = (extra or {}).get("dates")
            if dates and "year" in comp["conflicts"]:
                L += ["", "CrossRef dates for this DOI: " + ", ".join(f"{k} = {'-'.join(map(str, v))}" for k, v in dates.items())]
            L += ["", "Fields that agree: " + ", ".join(f for f, e in comp["fields"].items() if e["outcome"] == "agree"), "",
                  "Decide each field: "
                  + " ".join(f"`{f}=primary|sciverse|\"custom value\"`" for f in comp["conflicts"]),
                  f"e.g. `decide {key} {comp['conflicts'][0]}=primary`"]
        L += ["", "<details><summary>Raw records</summary>", "",
              "```json", json.dumps({"primary": primary, "sciverse": secondary}, ensure_ascii=False, indent=1)[:6000],
              "```", "</details>"]
        (m.dir / "review.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    def decide(self, key: str, assignments: dict) -> dict:
        m = self._manifest(key)
        for f, v in assignments.items():
            if f == "reference":
                m.data["decisions"]["reference"] = {"choice": "ok"}
            elif v in ("primary", "crossref", "arxiv"):
                m.data["decisions"][f] = {"choice": "primary"}
            elif v == "sciverse":
                m.data["decisions"][f] = {"choice": "sciverse"}
            else:
                m.data["decisions"][f] = {"choice": "custom", "value": int(v) if f == "year" and str(v).isdigit() else v}
        m.data["stages"]["verify"] = {"status": "pending"}
        m.save()
        return m.data["decisions"]

    # ---- stage 2 ----
    def stage_fetch(self, m: Manifest) -> None:
        if m.status("fetch") in ("done", "skipped"):
            return
        src_dir = m.dir / "source"
        kind, ident = m.data["kind"], m.data["id"]
        meta = json.loads((m.dir / "metadata.json").read_text(encoding="utf-8"))["record"]
        # a PDF the user supplied always wins (they may have a better copy)
        user_pdf = self._find_inbox_pdf(m.data["key"], meta.get("doi") or ident)
        if user_pdf and user_pdf.read_bytes()[:5] != b"%PDF-":
            m.set("fetch", "needs_user", kind="manual_download",
                  reason=f"{user_pdf.name} in the inbox is not a PDF (often a saved HTML login page)",
                  links=self._download_links(m, meta, m.data.get("pmcid"), {"reason": "invalid file"}),
                  save_as=str(self.inbox / f"{m.data['key']}.pdf"))
            return
        if user_pdf:
            src_dir.mkdir(exist_ok=True)
            shutil.copyfile(user_pdf, src_dir / "paper.pdf")
            m.data.update(fulltext_source="user-pdf")
            m.set("fetch", "done", source="user-pdf", file=str(user_pdf.name))
            return
        if kind == "arxiv":
            src_dir.mkdir(exist_ok=True)
            url = meta.get("pdf_url") or f"https://arxiv.org/pdf/{ident}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": http.user_agent()})
                with http.urlopen(req, timeout=120) as r, open(src_dir / "paper.pdf", "wb") as f:
                    shutil.copyfileobj(r, f)
                m.data.update(fulltext_source="arxiv-pdf")
                m.set("fetch", "done", source="arxiv-pdf", url=url)
            except Exception as e:
                m.set("fetch", "failed", reason=f"arXiv download failed: {e}")
            return
        # PMC OA subset
        pmcid = m.data.get("pmcid")
        if not pmcid:
            conv = pmc.idconv(ident)
            pmcid = (conv or {}).get("pmcid")
            if conv:
                m.data.update(pmcid=pmcid, pmid=conv.get("pmid"))
        oa = pmc.oa_status(pmcid) if pmcid else {"in_oa_subset": False, "reason": "no PMCID"}
        if oa["in_oa_subset"]:
            got = pmc.download(oa["prefix"], src_dir)
            pm = oa["meta"] or {}
            m.data.update(fulltext_source="pmc-oa", pmc={"prefix": oa["prefix"], "license": pm.get("license_code"),
                                                          "is_retracted": pm.get("is_retracted")})
            m.set("fetch", "done", source="pmc-oa", prefix=oa["prefix"], files=len(got["files"]),
                  license=pm.get("license_code"), **({"warning": "PMC marks this article as RETRACTED"}
                                                    if pm.get("is_retracted") else {}))
            return
        # closed (or not in the OA subset): links only, never downloaded by us
        links = self._download_links(m, meta, pmcid, oa)
        m.set("fetch", "needs_user", kind="manual_download", reason=oa.get("reason"), links=links,
              save_as=str(self.inbox / f"{m.data['key']}.pdf"))

    def _download_links(self, m, meta, pmcid, oa) -> list[dict]:
        doi = meta.get("doi") or m.data["id"]
        links = [{"label": "Publisher (DOI)", "url": f"https://doi.org/{doi}"}]
        src = json.loads((m.dir / "sources.json").read_text(encoding="utf-8")) if (m.dir / "sources.json").is_file() else {}
        for l in (src.get("primary_extra") or {}).get("links", []):
            if "pdf" in (l.get("content-type") or "") and l.get("intended-application") in (None, "text-mining", "similarity-checking", "unspecified"):
                links.append({"label": f"Publisher full text ({l.get('content-type')})", "url": l["URL"]})
        if pmcid:
            links.append({"label": f"PMC ({oa.get('reason')}; free to read, not reusable as OA)",
                          "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"})
        sv_oa = (meta.get("oa_status") or "")
        if sv_oa and sv_oa != "closed":
            links.append({"label": f"Sciverse reports OA status '{sv_oa}' — a legal free copy may exist", "url": f"https://doi.org/{doi}"})
        seen, out = set(), []
        for l in links:
            if l["url"] not in seen:
                seen.add(l["url"]); out.append(l)
        return out

    def _find_inbox_pdf(self, key: str, doi: str) -> Optional[Path]:
        norm = lambda s: re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
        suffix = norm((doi or "").split("/", 1)[1]) if "/" in (doi or "") else ""
        # full key / DOI, or the DOI suffix alone (publisher downloads are often named
        # "acs.analchem.5c00277.pdf"); short suffixes are too ambiguous to use
        needles = {n for n in (norm(key), norm(doi)) if len(n) >= 10} | ({suffix} if len(suffix) >= 8 else set())
        hits = [p for p in sorted(self.inbox.iterdir())
                if p.is_file() and p.suffix.lower() == ".pdf" and any(n in norm(p.stem) for n in needles)]
        # exact "save as" name first, then the most recently saved match
        exact = [p for p in hits if p.name == f"{key}.pdf"]
        return exact[0] if exact else (max(hits, key=lambda p: p.stat().st_mtime) if hits else None)

    def attach_pdf(self, key: str, pdf: Path) -> Path:
        m = self._manifest(key)
        dst = self.inbox / f"{key}.pdf"
        shutil.copyfile(pdf, dst)
        m.data["stages"]["fetch"] = {"status": "pending"}
        m.save()
        return dst

    # ---- stage 3 ----
    def stage_parse(self, m: Manifest) -> None:
        if m.status("parse") in ("done", "skipped"):
            return
        src = m.dir / "source"
        if m.data.get("fulltext_source") == "pmc-oa":
            xml = next(iter(src.glob("PMC*.xml")), None)
            files = [p.name for p in src.iterdir()]
            md, missing = jats.convert_file(xml, files)
            img_tmp = m.dir / "_images_tmp"
            img_tmp.mkdir(exist_ok=True)
            for f in files:
                if f.lower().endswith(jats.IMG_EXT):
                    shutil.copyfile(src / f, img_tmp / f)
            v = layout.install_markdown(m.dir, md, img_tmp)
            shutil.rmtree(img_tmp, ignore_errors=True)
            supp = [f for f in files if not f.lower().endswith(jats.IMG_EXT + (".xml", ".json", ".pdf"))]
            if supp:
                (m.dir / "supplementary").mkdir(exist_ok=True)
                for f in supp:
                    shutil.copyfile(src / f, m.dir / "supplementary" / f)
            m.data["parser"] = "jats2md"
            (m.set("parse", "done", parser="jats2md", missing_graphics=missing[:20], supplementary=len(supp), **v)
             if v["ok"] else m.set("parse", "failed", parser="jats2md", **v))
            return
        pdf = src / "paper.pdf"
        if not pdf.is_file():
            m.set("parse", "failed", reason="no source PDF")
            return
        if not self.allow_upload:
            m.set("parse", "needs_user", kind="upload_not_allowed",
                  reason="parsing this PDF needs MinerU cloud (upload to mineru.net); rerun with uploads allowed")
            return
        parser = self.parse_pdf or _mineru_cloud_parse
        if parser is _mineru_cloud_parse and not credentials.mineru_token():
            m.set("parse", "needs_user", kind="missing_token",
                  reason=f"MinerU token not configured: add [mineru] token to {credentials.path()}")
            return
        if parser is _mineru_cloud_parse:
            if not self._notice_shown:
                self.say(UPLOAD_NOTICE)
                self._notice_shown = True
            gap = CLOUD_INTERVAL_S - (time.time() - self._last_upload)
            if gap > 0:
                time.sleep(gap)
        try:
            name, md, images = parser(pdf, m.dir / "parser", self.say)
        except Exception as e:
            m.set("parse", "failed", reason=str(e)[:500])
            return
        finally:
            self._last_upload = time.time()
        # check the parser output where it lies; only a passing result replaces index.md
        pre = layout.check_markdown(md, images.parent if images else m.dir / "parser")
        if not pre["ok"]:
            m.set("parse", "failed", parser=name, stage="pre-install check", **pre)
            return
        v = layout.install_markdown(m.dir, md, images)
        m.data["parser"] = name
        m.set("parse", "done" if v["ok"] else "failed", parser=name, **v)

    # ---- driver ----
    def _manifest(self, key: str) -> Manifest:
        p = self.papers / key / "manifest.json"
        if not p.is_file():
            raise KeyError(f"unknown paper key {key!r}")
        return Manifest(p.parent, json.loads(p.read_text(encoding="utf-8")))

    def run(self, until: str = "parse") -> dict:
        order = ("verify", "fetch", "parse")
        stop = order.index(until) + 1 if until in order else len(order)
        for rid, it in list(self.queue["items"].items()):
            if it["status"] == "skipped":
                continue
            m = self.stage_resolve(rid)
            if m is None:
                continue
            for st in order[:stop]:
                if m.first_open_stage() != st:
                    if m.status(st) in ("done", "skipped"):
                        continue
                    break
                self.say(f"[{m.data['key']}] {st} …")
                getattr(self, f"stage_{st}")(m)
                if m.status(st) != "done":
                    break
        return self.write_attention()

    def summary(self) -> dict:
        s = {"resolve_needs_user": [], "verify_needs_user": [], "manual_download": [], "failed": [], "done": [],
             "in_progress": [], "upload_not_allowed": []}
        for rid, it in self.queue["items"].items():
            if it["status"] in ("needs_user", "failed") and it.get("status") != "skipped":
                s["resolve_needs_user"].append(rid)
        for m in manifest.iter_manifests(self.out):
            k = m.data["key"]
            if all(st["status"] in ("done", "skipped") for st in m.data["stages"].values()) and \
                    any(st["status"] == "skipped" for st in m.data["stages"].values()):
                s.setdefault("skipped", []).append(k)
                continue
            open_st = m.first_open_stage()
            if open_st is None:
                s["done"].append(k)
                continue
            st = m.data["stages"][open_st]
            kind = (st.get("detail") or {}).get("kind")
            if st["status"] == "failed":
                s["failed"].append(k)
            elif kind == "manual_download":
                s["manual_download"].append(k)
            elif kind in ("upload_not_allowed", "missing_token"):
                s["upload_not_allowed"].append(k)
            elif st["status"] == "needs_user":
                s["verify_needs_user"].append(k)
            else:
                s["in_progress"].append(k)
        return s

    def exit_code(self) -> int:
        s = self.summary()
        if s["failed"]:
            return EXIT_ERROR
        if s["resolve_needs_user"] or s["verify_needs_user"] or s["upload_not_allowed"]:
            return EXIT_DECISION
        if s["manual_download"]:
            return EXIT_DOWNLOAD
        return EXIT_OK

    def write_attention(self) -> dict:
        s = self.summary()
        L = [f"# Needs attention — {now()}", "",
             f"done {len(s['done'])} · decisions {len(s['resolve_needs_user']) + len(s['verify_needs_user']) + len(s['upload_not_allowed'])} · "
             f"downloads {len(s['manual_download'])} · failed {len(s['failed'])}", ""]
        if s["resolve_needs_user"]:
            L += ["## Which paper did you mean?", ""]
            for rid in s["resolve_needs_user"]:
                it = self.queue["items"][rid]
                L.append(f"- `{rid}` {json.dumps(it['ref'], ensure_ascii=False)[:120]} — {it.get('reason')} "
                         f"→ see `review/resolve-{rid}.md`")
            L.append("")
        if s["verify_needs_user"]:
            L += ["## Metadata conflicts between the sources", ""]
            for k in s["verify_needs_user"]:
                d = self._manifest(k).detail("verify")
                L.append(f"- `{k}`: {', '.join(d.get('fields') or d.get('messages') or [])} → see `papers/{k}/review.md`")
            L.append("")
        if s["manual_download"]:
            L += ["## Please download these PDFs yourself", "",
                  f"Save each file as shown (or any name containing the DOI) into `{self.inbox}`, then run again.", ""]
            for k in s["manual_download"]:
                d = self._manifest(k).detail("fetch")
                title = json.loads((self.papers / k / "metadata.json").read_text(encoding="utf-8"))["record"].get("title", "")
                L.append(f"### `{k}` — {title[:140]}")
                L += [f"- {l['label']}: <{l['url']}>" for l in d.get("links", [])]
                L += [f"- save as: `{Path(d.get('save_as', '')).name}`", ""]
        if s["upload_not_allowed"]:
            L += ["## Waiting to be parsed", ""]
            for k in s["upload_not_allowed"]:
                L.append(f"- `{k}`: {self._manifest(k).detail('parse').get('reason', '')}")
            L.append("")
        if s["failed"]:
            L += ["## Failed", ""]
            for k in s["failed"]:
                m = self._manifest(k)
                st = m.first_open_stage()
                L.append(f"- `{k}` at {st}: {(m.detail(st) or {}).get('reason', '')}")
        (self.out / "NEEDS_ATTENTION.md").write_text("\n".join(L) + "\n", encoding="utf-8")
        return s


def _mineru_cloud_parse(pdf: Path, work: Path, say) -> tuple[str, str, Optional[Path]]:
    from . import mineru_cloud
    say(f"  uploading {pdf.name} to MinerU cloud (mineru.net) for parsing")
    client = mineru_cloud.MinerUCloudClient(log=say)
    extracted = client.parse_pdf(pdf, work)
    md_path = mineru_cloud.find_markdown(extracted)
    if md_path is None:
        raise RuntimeError("MinerU result contains no Markdown")
    imgs = md_path.parent / "images"
    return "mineru-cloud", md_path.read_text(encoding="utf-8"), imgs if imgs.is_dir() else None
