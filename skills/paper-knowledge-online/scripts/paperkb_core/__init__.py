"""paperkb_core — shared, rule-based core of the paper-knowledge skills.

Design constraints (carried over from the original skill, confirmed 2026-10-25):
* stdlib only — the skill directory is copied into agent environments, so no
  third-party dependency may be assumed;
* retrieval and verification are pure rules — no LLM judgement anywhere;
* every external source degrades gracefully (a failing source is reported,
  never fatal to the others);
* secrets are read from a credentials file and never printed or logged.
"""
__version__ = "0.1.0"
