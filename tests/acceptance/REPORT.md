# Acceptance run — 2026-10-25 (online skill, before real credentials)

Procedure: the skill directory was copied to a clean location and the commands in
`SKILL.md` were run as written (`doctor`, `add --file`, `run --no-upload`, then
`choose`/`decide`/`skip`/`add`, a PDF dropped into `inbox/`, `run` again).
Sciverse key from the legacy environment variable; no MinerU token yet, so the two
PDFs that need MinerU cloud stopped at "waiting to be parsed" as designed.
Raw per-reference results: `results/acceptance_run.json`.

## Outcome per reference (12 + 1 re-added)

| input | resolved as | stops | full text |
|---|---|---|---|
| DOI (Vasilopoulou 2021) | given | — | PMC OA → jats2md |
| PMCID PMC4449330 | ID converter → 10.1038/nmeth.3393 | — | PMC OA → jats2md |
| title (ASCII hyphen) + author + journal + year | CrossRef title search → 10.3390/metabo13040558 | — | PMC OA → jats2md |
| DOI URL (bbae013) | given | — | PMC OA → jats2md |
| arXiv title only (MetaboLLM) | arXiv title search → 2608.06253 | — | arXiv PDF; parse waits for MinerU token |
| closed DOI (Liu 2025) | given | manual download | user PDF (`acs.analchem.5c00277 (1).pdf`); parse waits for token |
| closed DOI (Schlotterbeck 2019) | given | manual download | pending (exit 3 path) |
| title + "Kong" | **ask**: Research Square preprint vs Nature Methods | resolve → user chose #2 | author manuscript, not OA subset → links |
| 10.1016/j.trac.2022.116825 | given | **year** 2023 vs 2022 (Sciverse = CrossRef `created`) → user: primary | links |
| 10.29296/25877313-2023-09-02 | given | **first author** `Volkov, A.` vs `А. Г. Волков` → user: primary | links |
| bioRxiv DOI | given | manual download | links |
| DOI of Chang 2023 + title of MS-DIAL | given | **reference mismatch** (title similarity 0.49) → user: skip, re-add correct DOI | — |

Stop counts: 1 candidate choice, 3 verification stops (1 year, 1 script/transliteration,
1 mistyped identifier), 5 manual downloads. On the 87-paper baseline the conflict rule
stops 2/87 (same two cases).

## JATS (PMC OA) vs stored MinerU cloud parse of the same paper

`results/jats_vs_mineru.json`. Word-multiset overlap 89–97 % in both directions; every
`<fig>` and `<table-wrap>` in the XML is in `index.md` with a resolving image. MinerU's
larger image counts come from splitting figures into panels and rendering formulas as
images. Words only in MinerU are mostly publisher page furniture (licence boilerplate,
"Check for updates", running headers) and LaTeX macro names; words only in JATS
include ligatures MinerU broke (`diferent`, `efect`).

## Problems found and fixed during the run

1. `NEEDS_ATTENTION.md` header did not count "waiting to be parsed" while the CLI did.
2. The mismatch review told the user to `skip` the paper, but `skip` only accepted a
   reference id that the review page does not show → `skip` now accepts the paper key.
3. Inbox matching required the full DOI; publisher downloads are named after the DOI
   suffix → suffix match allowed when ≥ 8 characters (a unit test keeps short
   suffixes from matching).

## Real credentials file (2026-10-25, second session)

The user created the credentials file (mode 600) under a granted folder; it was
passed via `PAPERKB_CREDENTIALS` and read only by the scripts. `doctor`: all three
entries "configured from file"; CrossRef, PMC ID converter, PMC OA cloud, arXiv,
MinerU and Sciverse all OK.

`run` with uploads allowed then parsed the two waiting PDFs:

| paper | source | MinerU server time | result |
|---|---|---|---|
| arXiv 2608.06253 (1.4 MB) | arxiv-pdf | ~30 s | 163,801 chars, 80 headings, 17 tables, 5/5 images resolve |
| Liu 2025, Anal Chem (4.4 MB) | user-pdf | ~13 s | 49,120 chars, 26/26 images; identical to the library's earlier MinerU cloud parse (100 % word overlap, same length) |

Final state of the run: 6 done, 5 waiting for manual download (all closed or
not in the OA subset), 0 decisions, 0 failed → exit 3, as designed.

Secret check: 372 files under the run directory and the repository scanned for the
two key values (comparison in-process, values never printed) — 0 hits.

### Bug found by the real upload, fixed

The first real upload failed with Alibaba OSS `403 SignatureDoesNotMatch`. The stdlib
port used `urllib`, which silently adds `Content-Type: application/x-www-form-urlencoded`
to every request with a body; the presigned URL is signed with an empty Content-Type
(the original `requests` client sent none). A switch to raw `http.client` then failed
in the sandbox because it ignores `HTTPS_PROXY`. Final fix: `http.put_raw` keeps
urllib (proxy support) with a handler that strips only the implicit header; a
socket-level test asserts no Content-Type is sent unless the caller sets one.
The offline fake transport had not caught this because it never inspected headers.

## Not yet covered

* Publication (licence, public repository) — needs the user's decision.
