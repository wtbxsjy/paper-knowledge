"""Field-by-field equivalence with the pre-split implementation.

Replays the 333 recorded exchanges of the private baseline (87 DOIs) through
paperkb_core and compares with what the original scripts returned.  Skipped
when the private baseline is not present (public CI).

    PAPERKB_BASELINE_DIR=/path/to/paper-knowledge-local/tests/baseline \
        python -m unittest tests.test_equivalence -v
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "tests"))

BASELINE = Path(os.environ.get("PAPERKB_BASELINE_DIR", "/nonexistent"))
VOLATILE = {"verified_at"}


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


@unittest.skipUnless((BASELINE / "outputs.jsonl").is_file(), "private baseline not available")
class Equivalence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from replay import Replay
        from paperkb_core import credentials, http
        cls.replay = Replay(BASELINE / "http")
        http.urlopen = cls.replay
        os.environ.pop("PAPERKB_CONTACT_EMAIL", None)
        os.environ["PAPERKB_CREDENTIALS"] = str(ROOT / "tests" / "fixtures" / "credentials.test.toml")
        credentials.reset_cache()
        cls.rows = [json.loads(l) for l in (BASELINE / "outputs.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_crossref_pubmed_naming(self):
        from paperkb_core import journals
        from paperkb_core.sources import crossref, pubmed
        bad = []
        for r in self.rows:
            cr = crossref.verify_by_doi(r["doi"])
            if _strip(cr) != _strip(r["crossref"]):
                bad.append(("crossref", r["doi"]))
                continue
            if cr and cr.get("title"):
                if _strip(pubmed.verify_by_pubmed(cr["title"])) != _strip(r.get("pubmed_by_title")):
                    bad.append(("pubmed", r["doi"]))
                if journals.abbreviate_journal(cr.get("journal") or "") != r["abbrev"]:
                    bad.append(("abbrev", r["doi"]))
                name = journals.build_clean_name_from_meta(
                    cr.get("first_author") or "", cr.get("year") or 0, cr.get("journal") or "", cr["title"])
                if name != r["clean_name"]:
                    bad.append(("clean_name", r["doi"]))
        self.assertEqual(bad, [], f"{len(bad)} mismatches; replay misses: {self.replay.misses[:3]}")

    def test_sciverse_lookup_and_enrich(self):
        from paperkb_core.sources import sciverse
        bad = []
        for r in self.rows:
            if _strip(sciverse.lookup_by_doi(r["doi"])) != _strip(r["sciverse"]):
                bad.append(("lookup", r["doi"]))
            if r.get("crossref") and _strip(sciverse.enrich_verified(dict(r["crossref"]))) != _strip(r["enriched"]):
                bad.append(("enrich", r["doi"]))
        self.assertFalse(sciverse.STATE.disabled, sciverse.STATE.reason)
        self.assertEqual(bad, [], f"{len(bad)} mismatches; replay misses: {self.replay.misses[:3]}")


if __name__ == "__main__":
    unittest.main()
