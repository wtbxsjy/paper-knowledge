"""Live contract test: Sciverse coverage on the archived 35-paper set.

Opt-in (network + key): PAPERKB_LIVE=1. The CSV is private (it contains
Sciverse-derived metrics); point PAPERKB_SCIVERSE_CSV at it.
Contract (from the 2026-09 measurements, two independent runs):
35/35 found, 0 found/year mismatches against the archive, keywords /
ORCID / citation_count / fwci present for 35/35.
"""
import csv, os, sys, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
CSV = Path(os.environ.get("PAPERKB_SCIVERSE_CSV", "/nonexistent"))


@unittest.skipUnless(os.environ.get("PAPERKB_LIVE") == "1" and CSV.is_file(), "live test disabled")
class SciverseCoverage(unittest.TestCase):
    def test_35_paper_contract(self):
        from paperkb_core.sources import sciverse as sv
        rows = list(csv.DictReader(CSV.open(encoding="utf-8-sig")))
        found = kw = orcid = cite = fwci = 0
        mism = []
        for row in rows:
            rec = sv.lookup_by_doi(row["doi"]); time.sleep(sv.REQ_INTERVAL)
            exp = str(row.get("sv_found", "")).strip().lower() == "true"
            if (rec is not None) != exp:
                mism.append(f"{row['doi']}: found {rec is not None} != {exp}")
            if rec:
                found += 1
                kw += bool(rec["keywords"]); orcid += bool(rec["author_orcids"])
                cite += rec["citation_count"] is not None; fwci += rec["fwci"] not in (None, "")
                if row.get("sv_year") and int(rec["year"]) != int(float(row["sv_year"])):
                    mism.append(f"{row['doi']}: year {rec['year']} != {row['sv_year']}")
            self.assertFalse(sv.STATE.disabled, sv.STATE.reason)
        print(f"\nsciverse live: n={len(rows)} found={found} keywords={kw} orcid={orcid} "
              f"citation_count={cite} fwci={fwci} calls={sv.STATE.calls}")
        self.assertEqual(mism, [])
        self.assertEqual((found, kw, orcid, cite, fwci), (35,) * 5)


if __name__ == "__main__":
    unittest.main()
