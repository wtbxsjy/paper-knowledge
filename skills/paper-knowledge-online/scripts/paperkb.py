#!/usr/bin/env python3
"""paper-knowledge (online) — reference -> verified metadata -> full text.

    paperkb.py doctor
    paperkb.py add     RUN_DIR (--ref "DOI | title | ..." ... | --file refs.txt|.csv|.jsonl)
    paperkb.py run     RUN_DIR [--until verify|fetch|parse] [--no-upload]
    paperkb.py status  RUN_DIR [--json]
    paperkb.py choose  RUN_DIR REF_ID (N | DOI | arXiv-id)
    paperkb.py decide  RUN_DIR PAPER_KEY field=primary|sciverse|"value" ...  (or reference=ok)
    paperkb.py attach  RUN_DIR PAPER_KEY path/to/file.pdf
    paperkb.py skip    RUN_DIR (REF_ID | PAPER_KEY)

Exit codes of ``run`` / ``status``:
  0 all done · 2 a decision is needed · 3 only manual downloads pending · 1 error

Credentials are read from a file (``$PAPERKB_CREDENTIALS`` or
``~/.config/paperkb/credentials.toml``); this program never prints them.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # vendored copy: scripts/paperkb_core/
from paperkb_core import credentials, http, pipeline  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")


def cmd_doctor(a) -> int:
    st = credentials.status()
    print(f"credentials file: {st['file']} ({'found' if st['file_exists'] else 'NOT FOUND'}"
          + ("" if st["file_private"] in (None, True) else ", WARNING: readable by others — chmod 600") + ")")
    for name, e in st["entries"].items():
        req = {"mineru.token": "needed to parse PDFs", "sciverse.api_key": "second verification source",
               "contact.email": "optional, polite-pool identification"}[name]
        print(f"  {name:18s} {'configured' if e['configured'] else 'missing':10s} "
              f"{('from ' + e['origin']) if e['origin'] else ''}  ({req})")
    checks = [
        ("CrossRef", "https://api.crossref.org/works/10.1038/s41598-023-46312-8", None),
        ("PMC ID converter", "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?ids=PMC3531190&format=json", None),
        ("PMC OA cloud", "https://pmc-oa-opendata.s3.amazonaws.com/PMC3531190.1/PMC3531190.1.json", None),
        ("arXiv API", "https://export.arxiv.org/api/query?id_list=2301.00001&max_results=1", None),
        ("MinerU API", "https://mineru.net/api/v4/extract-results/batch/healthcheck", "mineru"),
    ]
    bad = 0
    for name, url, auth in checks:
        if auth == "mineru" and not credentials.mineru_token():
            print(f"  {name:18s} {'skipped':8s} no token configured")
            continue
        req = urllib.request.Request(url, headers={"User-Agent": http.user_agent()})
        if auth == "mineru" and credentials.mineru_token():
            req.add_header("Authorization", f"Bearer {credentials.mineru_token()}")
        try:
            with http.urlopen(req, timeout=15) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            code = f"unreachable ({type(e).__name__})"
        # MinerU: any answer other than 401/403 means the token is accepted (the batch id is fake)
        ok = isinstance(code, int) and (code < 400 or (auth == "mineru" and code not in (401, 403)))
        bad += not ok
        note = " (token rejected)" if auth == "mineru" and code in (401, 403) else ""
        print(f"  {name:18s} {'ok' if ok else 'PROBLEM':8s} HTTP {code}{note}")
    if credentials.sciverse_api_key():
        from paperkb_core.sources import sciverse as sv
        rec = sv.lookup_by_doi("10.1038/s41598-023-46312-8")
        ok = rec is not None
        bad += not ok
        print(f"  {'Sciverse':18s} {'ok' if ok else 'PROBLEM':8s} {'' if ok else sv.STATE.reason or 'no record'}")
    if not credentials.mineru_token():
        print("note: without a MinerU token, papers that are not PMC OA stop before parsing.")
    return 0 if not bad else 1


def _run(a) -> pipeline.Run:
    return pipeline.Run(Path(a.run_dir), allow_upload=not getattr(a, "no_upload", False))


def cmd_add(a) -> int:
    refs = []
    for r in a.ref or []:
        x = pipeline.parse_reference_line(r)
        if x:
            refs.append(x)
    if a.file:
        refs += pipeline.read_references(Path(a.file))
    if not refs:
        print("nothing to add", file=sys.stderr)
        return 1
    run = _run(a)
    new = run.add(refs)
    for rid in new:
        print(rid, json.dumps(run.queue["items"][rid]["ref"], ensure_ascii=False))
    print(f"added {len(new)} (already queued: {len(refs) - len(new)})")
    return 0


def _print_status(run: pipeline.Run, s: dict) -> None:
    print(f"done {len(s['done'])} · need decision {len(s['resolve_needs_user']) + len(s['verify_needs_user']) + len(s['upload_not_allowed'])}"
          f" · manual download {len(s['manual_download'])} · failed {len(s['failed'])}")
    print(f"details: {run.out / 'NEEDS_ATTENTION.md'}")


def cmd_run(a) -> int:
    run = _run(a)       # the pipeline prints the upload notice once, before the first upload
    s = run.run(until=a.until)
    _print_status(run, s)
    return run.exit_code()


def cmd_status(a) -> int:
    run = _run(a)
    s = run.write_attention()
    if a.json:
        print(json.dumps(s, indent=1))
    else:
        _print_status(run, s)
    return run.exit_code()


def cmd_choose(a) -> int:
    print(_run(a).choose_candidate(a.ref_id, a.choice))
    return 0


def cmd_skip(a) -> int:
    print("skipped:", ", ".join(_run(a).skip(a.ref_id)))
    return 0


def cmd_decide(a) -> int:
    pairs = {}
    for kv in a.assignments:
        if "=" not in kv:
            print(f"expected field=value, got {kv!r}", file=sys.stderr)
            return 1
        k, v = kv.split("=", 1)
        pairs[k.strip()] = v.strip().strip('"')
    print(_run(a).decide(a.key, pairs))
    return 0


def cmd_attach(a) -> int:
    p = Path(a.pdf)
    if p.read_bytes()[:5] != b"%PDF-":
        print(f"{p} is not a PDF", file=sys.stderr)
        return 1
    print(_run(a).attach_pdf(a.key, p))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("doctor").set_defaults(f=cmd_doctor)
    p = sp.add_parser("add"); p.add_argument("run_dir"); p.add_argument("--ref", action="append")
    p.add_argument("--file"); p.set_defaults(f=cmd_add)
    p = sp.add_parser("run"); p.add_argument("run_dir"); p.add_argument("--until", default="parse",
                                                                       choices=["verify", "fetch", "parse"])
    p.add_argument("--no-upload", action="store_true"); p.set_defaults(f=cmd_run)
    p = sp.add_parser("status"); p.add_argument("run_dir"); p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_status)
    p = sp.add_parser("choose"); p.add_argument("run_dir"); p.add_argument("ref_id"); p.add_argument("choice")
    p.set_defaults(f=cmd_choose)
    p = sp.add_parser("skip"); p.add_argument("run_dir"); p.add_argument("ref_id"); p.set_defaults(f=cmd_skip)
    p = sp.add_parser("decide"); p.add_argument("run_dir"); p.add_argument("key"); p.add_argument("assignments", nargs="+")
    p.set_defaults(f=cmd_decide)
    p = sp.add_parser("attach"); p.add_argument("run_dir"); p.add_argument("key"); p.add_argument("pdf")
    p.set_defaults(f=cmd_attach)
    a = ap.parse_args(argv)
    try:
        return a.f(a)
    except credentials.CredentialFileError as e:
        print(f"credentials: {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
