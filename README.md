# paper-knowledge

Rule-based pipeline from a paper reference (DOI / arXiv / PMID / title + author,
journal, year) to verified metadata and full-text Markdown.

* `skills/paper-knowledge-online/` — the agent skill (self-contained; copy the
  directory). Start with its `SKILL.md`.
* `core/paperkb_core/` — the shared library (standard library only). The skill
  carries a vendored copy; `python3 tools/sync_core.py skills/paper-knowledge-online/scripts`
  refreshes it, `--check` verifies it.
* `tests/` — `python3 -m unittest discover -s tests`. Offline by default; live
  contract tests need `PAPERKB_LIVE=1`.

## Design rules

* No LLM decides anything: matching, comparison and source selection are explicit
  rules, and every case they cannot settle goes to the user with the evidence.
* Two metadata sources: CrossRef (arXiv for preprints) and Sciverse. Sciverse fills
  gaps and is compared, never silently preferred.
* Full text is fetched automatically only from the PMC Open Access subset and arXiv.
  Everything else is a link for the user to download.
* Secrets live in a file read by the scripts (`credentials.example.toml`), never in
  the conversation or the logs.

## Licence

MIT — see `LICENSE`.
