import io, json, os, stat, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from paperkb_core import compare, credentials, ids, layout, manifest  # noqa: E402


class Ids(unittest.TestCase):
    def test_classify(self):
        cases = {
            "https://doi.org/10.1038/S41598-023-46312-8": ("doi", "10.1038/s41598-023-46312-8"),
            "doi:10.1093/bib/bbae013": ("doi", "10.1093/bib/bbae013"),
            "10.48550/arXiv.2301.01234": ("arxiv", "2301.01234"),
            "https://arxiv.org/abs/2608.06253v2": ("arxiv", "2608.06253"),
            "arXiv:hep-th/9901001v1": ("arxiv", "hep-th/9901001"),
            "PMC1234567": ("pmcid", "PMC1234567"),
            "PMID: 31234567": ("pmid", "31234567"),
            "not an id": ("", ""),
        }
        for s, want in cases.items():
            self.assertEqual(ids.classify(s), want, s)

    def test_extract_doi(self):
        self.assertEqual(ids.extract_doi_from_text("see https://doi.org/10.1000/xyz123)."), "10.1000/xyz123")


CR = {"source": "crossref", "title": "Denoising <i>Search</i> doubles annotations",
      "first_author": "Fazly Bazzaz, Bibi Sedigheh", "year": 2023,
      "journal": "Analytical Chemistry", "journal_abbrev": "Anal. Chem."}
SV = {"source": "sciverse", "title": "Denoising Search Doubles Annotations",
      "authors": ["Bibi Sedigheh Fazly Bazzaz"], "year": 2023,
      "venue": "Anal Chem, 2023, Vol 95, Issue 1, p1", "venue_trustworthy": True}


class Compare(unittest.TestCase):
    def test_format_differences_agree(self):
        c = compare.compare(CR, SV)
        self.assertEqual(c["status"], "agree", c)

    def test_year_conflict_stops_with_evidence(self):
        c = compare.compare(CR, dict(SV, year=2022),
                            {"dates": {"issued": [2023, 1], "published-online": [2022, 11, 3]}})
        self.assertEqual(c["status"], "needs_review")
        self.assertEqual(c["conflicts"], ["year"])
        self.assertIn("published-online", c["fields"]["year"]["note"])
        with self.assertRaises(ValueError):
            compare.resolve(CR, SV, c)
        rec = compare.resolve(CR, SV, c, {"year": {"choice": "sciverse"}})
        self.assertEqual((rec["year"], rec["field_provenance"]["year"]), (2022, "user:sciverse"))

    def test_untrustworthy_venue_is_not_a_conflict(self):
        c = compare.compare(CR, dict(SV, venue="ChemRxivActionsSearch in PubMed", venue_trustworthy=False))
        self.assertEqual(c["fields"]["journal"]["outcome"], "unreliable")
        self.assertEqual(c["status"], "agree")

    def test_fill_from_secondary(self):
        c = compare.compare(dict(CR, journal=""), SV)
        self.assertEqual(c["fields"]["journal"]["outcome"], "fill")
        self.assertEqual(compare.resolve(dict(CR, journal=""), SV, c)["field_provenance"]["journal"], "sciverse")

    def test_single_source(self):
        self.assertEqual(compare.compare(CR, None)["status"], "single_source")


SECRET = "s3cr3t-value-never-printed"


class Credentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "c.toml"
        self.p.write_text(f'[mineru]\ntoken = "{SECRET}"\n[sciverse]\napi_key = "{SECRET}2"\n')
        self.env = {k: os.environ.pop(k) for k in
                    ("MINERU_TOKEN", "SCIVERSE_API_KEY", "SCIVERSE_API_TOKEN", "PAPERKB_CONTACT_EMAIL") if k in os.environ}
        os.environ["PAPERKB_CREDENTIALS"] = str(self.p)
        credentials.reset_cache()

    def tearDown(self):
        os.environ.pop("PAPERKB_CREDENTIALS", None)
        os.environ.update(self.env)
        credentials.reset_cache()
        self.tmp.cleanup()

    def test_values_and_status_never_leak(self):
        os.chmod(self.p, 0o600)
        self.assertEqual(credentials.mineru_token(), SECRET)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            st = credentials.status()
            print(json.dumps(st))
        self.assertNotIn(SECRET, out.getvalue())
        self.assertTrue(st["entries"]["mineru.token"]["configured"])
        self.assertEqual(st["entries"]["contact.email"]["configured"], False)
        self.assertTrue(st["file_private"])

    def test_permission_warning(self):
        os.chmod(self.p, 0o644)
        err = io.StringIO()
        with redirect_stderr(err):
            credentials.mineru_token()
        self.assertIn("chmod 600", err.getvalue())
        self.assertNotIn(SECRET, err.getvalue())

    def test_bad_toml_message_has_no_content(self):
        self.p.write_text(f'[mineru]\ntoken = "{SECRET}\n')
        credentials.reset_cache()
        with self.assertRaises(credentials.CredentialFileError) as cm:
            credentials.mineru_token()
        self.assertNotIn(SECRET, str(cm.exception))


class ManifestLayout(unittest.TestCase):
    def test_resume_and_validate(self):
        with tempfile.TemporaryDirectory() as t:
            key = manifest.paper_key("doi", "10.1038/s41598-023-46312-8")
            self.assertEqual(key, "doi-10.1038_s41598-023-46312-8")
            d = Path(t) / "papers" / key
            m = manifest.Manifest.load_or_create(d, key, {"doi": "10.1038/s41598-023-46312-8"})
            m.set("resolve", "done"); m.set("verify", "needs_user", kind="resolve_conflict", fields=["year"])
            m2 = manifest.Manifest.load_or_create(d, key, {"title": "x"})
            self.assertEqual(m2.first_open_stage(), "verify")
            self.assertEqual(len(m2.data["references"]), 2)
            (d / "img").mkdir()
            v = layout.install_markdown(d, "# T\n" + "x" * 600 + "\n![](images/a.jpg)\n")
            self.assertFalse(v["ok"]); self.assertEqual(v["missing_images"], ["images/a.jpg"])
            (d / "images").mkdir(); (d / "images" / "a.jpg").write_bytes(b"\xff")
            self.assertTrue(layout.validate(d)["ok"])


if __name__ == "__main__":
    unittest.main()
