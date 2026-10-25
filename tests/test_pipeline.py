"""Pipeline behaviour with all network sources replaced by fakes."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from paperkb_core import credentials, pipeline, resolve  # noqa: E402
from paperkb_core.sources import arxiv, crossref, pmc, sciverse  # noqa: E402

JATS = """<article><front><article-meta><title-group><article-title>Open paper</article-title></title-group>
<abstract><p>{}</p></abstract></article-meta></front><body><sec><title>Intro</title><p>Text.</p>
<fig><label>Figure 1</label><caption><p>Cap.</p></caption><graphic xlink:href="f1" xmlns:xlink="http://www.w3.org/1999/xlink"/></fig>
</sec></body></article>""".format("word " * 200)


def cr_msg(doi, title, year, journal="J Test"):
    return {"DOI": doi, "title": [title], "author": [{"family": "Doe", "given": "Jane"}],
            "issued": {"date-parts": [[year]]}, "container-title": [journal], "short-container-title": [journal],
            "type": "journal-article"}


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "run"
        self.saved = {(m, n): getattr(m, n) for m, n in [
            (crossref, "fetch_message"), (crossref, "search_bibliographic"), (sciverse, "lookup_by_doi"),
            (pmc, "idconv"), (pmc, "oa_status"), (pmc, "download"), (arxiv, "lookup"), (arxiv, "search_title")]}
        self.msgs = {"10.1/open": cr_msg("10.1/open", "Open paper", 2020),
                     "10.1/closed": cr_msg("10.1/closed", "Closed paper", 2021),
                     "10.1/conflict": cr_msg("10.1/conflict", "Conflict paper", 2023)}
        crossref.fetch_message = lambda d: self.msgs.get(d)
        crossref.search_bibliographic = lambda *a, **k: []
        arxiv.search_title = lambda *a, **k: []
        arxiv.lookup = lambda i: None
        sciverse.lookup_by_doi = lambda d: (dict(title=self.msgs[d]["title"][0], authors=["Jane Doe"],
                                                 year=2022 if d == "10.1/conflict" else self.msgs[d]["issued"]["date-parts"][0][0],
                                                 venue="J Test", venue_trustworthy=True, doi=d, source="sciverse",
                                                 keywords=["k"], citation_count=3) if d in self.msgs else None)
        pmc.idconv = lambda d: {"doi": d, "pmcid": "PMC1" if d == "10.1/open" else None, "pmid": None}
        pmc.oa_status = lambda p: {"in_oa_subset": True, "prefix": "PMC1.1", "meta": {"license_code": "CC BY"}, "reason": "ok"}

        def dl(prefix, dest, include_pdf=True):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "PMC1.1.xml").write_text(JATS); (dest / "f1.jpg").write_bytes(b"\xff")
            return {"files": ["PMC1.1.xml", "f1.jpg"]}
        pmc.download = dl
        self.parsed = []

        def fake_parse(pdf, work, say):
            self.parsed.append(pdf)
            work.mkdir(parents=True, exist_ok=True)
            return "fake", "# t\n" + "w " * 400, None
        self.run = pipeline.Run(self.out, parse_pdf=fake_parse, log_fn=lambda *_: None)

    def tearDown(self):
        for (m, n), v in self.saved.items():
            setattr(m, n, v)
        self.tmp.cleanup()

    def test_full_flow(self):
        self.run.add([{"id": "10.1/open"}, {"id": "10.1/closed"}, {"id": "10.1/conflict"}])
        s = self.run.run()
        self.assertEqual(s["done"], ["doi-10.1_open"])
        self.assertEqual(s["manual_download"], ["doi-10.1_closed"])
        self.assertEqual(s["verify_needs_user"], ["doi-10.1_conflict"])
        self.assertEqual(self.run.exit_code(), pipeline.EXIT_DECISION)
        p = self.out / "papers" / "doi-10.1_open"
        self.assertIn("![Figure 1](images/f1.jpg)", (p / "index.md").read_text())
        meta = json.loads((p / "metadata.json").read_text())["record"]
        self.assertEqual(meta["citation_count"], 3)
        self.assertIn("Conflict paper", (self.out / "papers/doi-10.1_conflict/review.md").read_text())
        # decision is remembered; rerun asks nothing
        self.run.decide("doi-10.1_conflict", {"year": "sciverse"})
        s = self.run.run()
        self.assertEqual(json.loads((self.out / "papers/doi-10.1_conflict/metadata.json").read_text())["record"]["year"], 2022)
        self.assertEqual(s["verify_needs_user"], [])
        self.assertEqual(self.run.exit_code(), pipeline.EXIT_DOWNLOAD)
        # user drops PDFs into the inbox (browser-style names)
        for k in ("closed (1)", "doi-10.1_conflict"):
            (self.out / "inbox" / f"{k}.pdf").write_bytes(b"%PDF-1.4 x")
        s = self.run.run()
        self.assertEqual(s["manual_download"], ["doi-10.1_closed"], "a 6-char DOI suffix is too short to match")
        (self.out / "inbox" / "10.1_closed (1).pdf").write_bytes(b"%PDF-1.4 x")
        s = self.run.run()
        self.assertEqual(sorted(s["done"]), ["doi-10.1_closed", "doi-10.1_conflict", "doi-10.1_open"])
        self.assertEqual(self.run.exit_code(), pipeline.EXIT_OK)
        n = len(self.parsed)
        self.run.run()
        self.assertEqual(len(self.parsed), n, "finished papers are not parsed again")

    def test_same_paper_twice_is_one_directory(self):
        self.run.add([{"id": "10.1/open"}, {"id": "https://doi.org/10.1/OPEN"}])
        self.run.run()
        self.assertEqual([p.name for p in (self.out / "papers").iterdir()], ["doi-10.1_open"])

    def test_no_upload_stops_before_parse(self):
        r = pipeline.Run(self.out, allow_upload=False, log_fn=lambda *_: None)
        r.add([{"id": "10.1/closed"}]); r.run()
        (self.out / "inbox" / "doi-10.1_closed.pdf").write_bytes(b"%PDF-1.4")
        s = r.run()
        self.assertEqual(s["upload_not_allowed"], ["doi-10.1_closed"])

    def test_missing_token_stops_before_upload(self):
        os.environ["PAPERKB_CREDENTIALS"] = "/nonexistent"; credentials.reset_cache()
        saved = {k: os.environ.pop(k) for k in ("MINERU_TOKEN",) if k in os.environ}
        leg = dict(credentials._LEGACY_FILES); credentials._LEGACY_FILES.clear()
        try:
            r = pipeline.Run(self.out, log_fn=lambda *_: None)
            r.add([{"id": "10.1/closed"}]); r.run()
            (self.out / "inbox" / "doi-10.1_closed.pdf").write_bytes(b"%PDF-1.4")
            r.run()
            m = json.loads((self.out / "papers/doi-10.1_closed/manifest.json").read_text())
            self.assertEqual(m["stages"]["parse"]["detail"]["kind"], "missing_token")
        finally:
            credentials._LEGACY_FILES.update(leg); os.environ.pop("PAPERKB_CREDENTIALS"); os.environ.update(saved)
            credentials.reset_cache()

    def test_html_in_inbox_rejected(self):
        self.run.add([{"id": "10.1/closed"}]); self.run.run()
        (self.out / "inbox" / "doi-10.1_closed.pdf").write_text("<html>")
        self.run.run()
        m = json.loads((self.out / "papers/doi-10.1_closed/manifest.json").read_text())
        self.assertIn("not a PDF", m["stages"]["fetch"]["detail"]["reason"])

    def test_reference_lines(self):
        self.assertEqual(pipeline.parse_reference_line("Some title | Doe | Nature | 2020"),
                         {"title": "Some title", "authors": "Doe", "journal": "Nature", "year": 2020})
        self.assertEqual(pipeline.parse_reference_line("10.1/x"), {"id": "10.1/x"})
        self.assertIsNone(pipeline.parse_reference_line("# comment"))
        self.assertEqual(pipeline.parse_reference_line("10.1/x | A title"), {"id": "10.1/x", "title": "A title"})


class Vendored(unittest.TestCase):
    def test_online_skill_copy_in_sync(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools/sync_core.py"),
                            str(ROOT / "skills/paper-knowledge-online/scripts"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
