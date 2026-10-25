"""Resolution rules on synthetic candidates (no network)."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from paperkb_core import resolve  # noqa: E402
from paperkb_core.sources import arxiv, crossref, pmc  # noqa: E402

T = "Denoising Search doubles the number of metabolite and exposome annotations in human plasma"


def item(doi, title, family, issued, print_=None, online=None, typ="journal-article", journal="Nature Methods"):
    d = {"DOI": doi, "title": [title], "author": [{"family": family, "given": "H."}],
         "issued": {"date-parts": [issued]}, "container-title": [journal], "type": typ}
    if print_: d["published-print"] = {"date-parts": [print_]}
    if online: d["published-online"] = {"date-parts": [online]}
    return d


class Resolve(unittest.TestCase):
    def setUp(self):
        self.orig = (crossref.search_bibliographic, arxiv.search_title, pmc.idconv)
        arxiv.search_title = lambda *a, **k: []
        self.items = []
        crossref.search_bibliographic = lambda *a, **k: list(self.items)

    def tearDown(self):
        crossref.search_bibliographic, arxiv.search_title, pmc.idconv = self.orig

    def test_identifiers(self):
        self.assertEqual(resolve.resolve({"id": "https://doi.org/10.1/ABC"})["id"], "10.1/abc")
        self.assertEqual(resolve.resolve({"id": "arXiv:2608.06253v2"})["kind"], "arxiv")
        pmc.idconv = lambda x: {"doi": "10.1/x", "pmcid": "PMC1", "pmid": x}
        self.assertEqual(resolve.resolve({"pmid": "123"})["via"], "idconv:pmid")

    def test_unique_title_resolves(self):
        self.items = [item("10.1/a", T, "Kong", [2025, 5])]
        r = resolve.resolve({"title": T, "authors": "Kong"})
        self.assertEqual((r["status"], r["id"]), ("resolved", "10.1/a"))

    def test_truncated_title_contained(self):
        self.items = [item("10.1/a", T + " using an Orbitrap Astral", "Kong", [2025])]
        self.assertEqual(resolve.resolve({"title": T, "authors": "Kong"})["status"], "resolved")

    def test_preprint_and_journal_version_asks(self):
        self.items = [item("10.21203/rs", T, "Kong", [2024], typ="posted-content", journal=""),
                      item("10.1038/j", T, "Kong", [2025])]
        r = resolve.resolve({"title": T, "authors": "Kong", "year": 2025})
        self.assertEqual(r["status"], "needs_user")      # neighbour within 1 year -> ask
        self.assertEqual(len(r["candidates"]), 2)

    def test_year_separates_distant_records(self):
        self.items = [item("10.1/2013", "GenBank", "Benson", [2013, 11]),
                      item("10.1/2004", "GenBank", "Benson", [2004, 1])]
        r = resolve.resolve({"title": "GenBank", "year": 2013})
        self.assertEqual((r["status"], r["id"]), ("resolved", "10.1/2013"))

    def test_incomplete_dates_stay_ambiguous(self):
        # real case: gks1195 has no print date in CrossRef (print year 2013), gkt1030 online 2013
        self.items = [item("10.1093/nar/gkt1030", "GenBank", "Benson", [2013, 11], print_=[2014, 1], online=[2013, 11]),
                      item("10.1093/nar/gks1195", "GenBank", "Benson", [2012, 11], online=[2012, 11])]
        self.assertEqual(resolve.resolve({"title": "GenBank", "year": 2013})["status"], "needs_user")

    def test_author_mismatch_asks(self):
        self.items = [item("10.1/a", T, "Kong", [2025])]
        r = resolve.resolve({"title": T, "authors": "Smith"})
        self.assertEqual(r["status"], "needs_user")
        self.assertIn("first_author", r["reason"])

    def test_supplement_components_ignored(self):
        self.items = [item("10.1/a.s001", T, "Kong", [2025], typ="component"), item("10.1/a", T, "Kong", [2025])]
        self.assertEqual(resolve.resolve({"title": T})["id"], "10.1/a")

    def test_reference_check(self):
        self.assertIsNotNone(resolve.reference_check({"doi": "10.1/x", "title": "Totally different"}, {"title": T}))
        self.assertIsNone(resolve.reference_check({"doi": "10.1/x", "title": T}, {"title": T}))


if __name__ == "__main__":
    unittest.main()
