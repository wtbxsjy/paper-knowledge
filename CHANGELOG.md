# Changelog

## 0.1.0 — 2026-10-25

First public release of the online skill `paper-knowledge-online` and its shared
library `paperkb_core`.

* Reference resolution from DOI, arXiv id/URL, PMID, PMCID or title (+ author,
  journal, year); ambiguous titles stop with a candidate table.
* Metadata verification against CrossRef (arXiv for preprints) and Sciverse with
  normalised field comparison; disagreements stop with the evidence for the user.
* Full text from the PMC Open Access subset (PMC Cloud Service, JATS → Markdown with
  figures) and arXiv; other papers get download links and an inbox folder.
* PDF parsing with the MinerU cloud API (serial, upload notice, `--no-upload`).
* Resumable runs, fixed exit codes, `doctor` health check; credentials from a file.
