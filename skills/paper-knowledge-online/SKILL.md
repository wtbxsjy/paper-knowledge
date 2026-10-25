---
name: paper-knowledge-online
description: >
  Turn paper references (DOI, arXiv id, PMID/PMCID, or title + author/journal/year)
  into verified metadata and full-text Markdown. Metadata is checked against two
  sources (CrossRef or arXiv, plus Sciverse); disagreements are shown to the user,
  never resolved silently. Full text comes from the PMC Open Access subset (JATS XML)
  or arXiv; for other papers the user downloads the PDF from the links given, and
  it is parsed with the MinerU cloud API. Use when a user wants papers collected,
  verified, or converted to Markdown for reading or a knowledge base.
compatibility: Python 3.11+, standard library only. Network access required.
metadata:
  version: 0.1.0
  allowed_domains:
    - api.crossref.org
    - api.sciverse.space
    - pmc.ncbi.nlm.nih.gov
    - pmc-oa-opendata.s3.amazonaws.com
    - export.arxiv.org
    - arxiv.org
    - mineru.net
    - mineru.oss-cn-shanghai.aliyuncs.com
    - cdn-mineru.openxlab.org.cn
---

# paper-knowledge (online)

One command line tool, `scripts/paperkb.py`, runs four stages per paper and can be
re-run at any time; finished work is never repeated.

| stage | what happens | stops for the user when |
|---|---|---|
| resolve | identifier normalised; a title is matched against CrossRef/arXiv | no candidate, or several plausible ones (e.g. preprint and journal version) |
| verify | CrossRef (arXiv for preprints) and Sciverse compared field by field | title, first author, year or journal differ after normalisation |
| fetch | PMC OA subset → JATS XML + figures; arXiv → PDF | anything else: download links are listed, the user saves the PDF |
| parse | XML → Markdown locally; PDF → MinerU cloud (uploads the PDF) | no MinerU token, or uploads not allowed |

Retrieval and verification are rule-based. Do not substitute your own judgement for
a stop: the stop exists so the user decides.

## Rules for the agent

1. **Credentials.** Never open, `cat`, print or ask the user to paste the credentials
   file or any key. Use `paperkb.py doctor` to learn what is configured. If something
   is missing, tell the user to create the file from `credentials.example.toml`
   (path shown by `doctor`) and `chmod 600` it themselves.
2. **Uploads.** Before the first `run` that may parse a PDF, tell the user that PDFs
   without PMC OA XML are uploaded to mineru.net. If they object, use `--no-upload`.
3. **Downloads.** Never download a PDF yourself from publisher links, shadow libraries
   or anywhere else. Only PMC OA and arXiv are fetched automatically. For the rest,
   show the user the links from `NEEDS_ATTENTION.md` and wait.
4. **Decisions.** When `run` exits with 2, show the user the evidence table from the
   review file **as it is** (both values, the CrossRef dates, the notes) and ask which
   to use. Record exactly what they say with `choose` / `decide`. Do not pick a value
   because it looks more plausible.
5. Report results from the files (`metadata.json`, `manifest.json`), not from memory.

## Setup (once)

```bash
S=<skill dir>/scripts/paperkb.py
python3 $S doctor          # credential presence + endpoint health; never prints secrets
```

## Workflow

```bash
RUN=~/papers/run-2026-10          # any directory; one per collection
python3 $S add $RUN --ref "10.1038/s41598-023-46312-8" \
                    --ref "Q-RAI data-independent acquisition for lipidomic quantitative profiling | Chang | Scientific Reports | 2023"
python3 $S add $RUN --file refs.txt          # one per line, same "title | author | journal | year" form;
                                             # .csv (columns id/doi/title/authors/journal/year) or .jsonl also work
python3 $S run $RUN                          # add --no-upload to stop before any MinerU upload
echo $?                                      # 0 done · 2 decision needed · 3 downloads pending · 1 error
```

Everything that waits on the user is listed in `$RUN/NEEDS_ATTENTION.md`.

### Exit 2 — a decision is needed

* **Which paper?** `$RUN/review/resolve-<ref_id>.md` lists the candidates with
  per-field checks. Then `python3 $S choose $RUN <ref_id> <number | DOI | arXiv id>`
  or `python3 $S skip $RUN <ref_id>`.
* **Sources disagree.** `$RUN/papers/<key>/review.md` shows both values.
  Then `python3 $S decide $RUN <key> year=primary` (or `=sciverse`, or `year=2023`
  for a value the user gives). `primary` is CrossRef, or arXiv for preprints.
  `reference=ok` confirms an identifier whose record does not match the title the
  user typed; if the identifier was wrong, `python3 $S skip $RUN <key>` and add the
  paper again with the right one.
* **Waiting to be parsed** (no token / `--no-upload`): fix the cause, run again.

Then `run` again. Decisions are stored and never asked twice.

### Exit 3 — PDFs to download

Give the user the links under "Please download these PDFs yourself". They save the
file into `$RUN/inbox/` — the suggested name, or any name that contains the DOI
(`10.1016_j.trac.2022.116825 (1).pdf` is fine, and so is the DOI part after the slash when it
is at least 8 characters, e.g. `acs.analchem.5c00277.pdf`). Alternatively
`python3 $S attach $RUN <key> /path/file.pdf`. Then `run` again. A saved HTML login
page is detected and reported, not parsed.

## Output

```
$RUN/papers/<key>/            key = doi-<doi with / → _>  or  arxiv-<id>
  index.md                    full text; figures as images/...
  images/
  metadata.json               {"record": verified fields + field_provenance + Sciverse extras,
                               "comparison": per-field outcome}
  manifest.json               stage status, decisions, fulltext_source, parser
  source/                     what the text was made from (PMC XML, PDF)
  supplementary/              PMC supplementary files, when present
```

`fulltext_source` is `pmc-oa`, `arxiv-pdf` or `user-pdf`; `parser` is `jats2md` or
`mineru-cloud`. `field_provenance` says, per field, whether the value came from
CrossRef, arXiv, Sciverse or a user decision.

## Known limits

* PMC "author manuscripts" and papers that are free to read but not in the OA subset
  are not fetched (user decision: OA subset and arXiv only). Their PMC page is listed
  as a download link.
* bioRxiv/medRxiv/Research Square preprints are not fetched; links are given.
* Sciverse year can be the online year while CrossRef gives the print year; this is
  shown as a conflict on purpose.
* MinerU cloud upload: strictly one at a time, 5 s apart; large PDFs take minutes.
