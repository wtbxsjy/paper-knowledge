"""Credentials from a file, never from the conversation.

Lookup order for the file:
  1. ``$PAPERKB_CREDENTIALS`` (path), else
  2. ``~/.config/paperkb/credentials.toml``

File format (TOML, chmod 600)::

    [mineru]
    token = "..."
    [sciverse]
    api_key = "..."
    [contact]
    email = ""          # optional; sent to CrossRef/NCBI only if non-empty

Legacy environment variables are honoured as a fallback so existing setups
keep working: ``MINERU_TOKEN``, ``SCIVERSE_API_KEY`` / ``SCIVERSE_API_TOKEN``,
``PAPERKB_CONTACT_EMAIL``; plus the legacy MinerU token file
``~/.config/mineru/token``.

Rules enforced here:
* values are never printed, logged or put into exception messages;
* ``status()`` reports only configured / missing and where it came from;
* a file readable by group/others triggers a warning (like ssh does for keys).
"""
from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from pathlib import Path

DEFAULT_PATH = Path("~/.config/paperkb/credentials.toml").expanduser()
ENV_PATH = "PAPERKB_CREDENTIALS"

# (section, key) -> legacy env vars, in priority order
_ENV_FALLBACK = {
    ("mineru", "token"): ("MINERU_TOKEN",),
    ("sciverse", "api_key"): ("SCIVERSE_API_KEY", "SCIVERSE_API_TOKEN"),
    ("contact", "email"): ("PAPERKB_CONTACT_EMAIL",),
}
_LEGACY_FILES = {("mineru", "token"): Path("~/.config/mineru/token").expanduser()}

_cache: dict | None = None
_cache_path: Path | None = None
_warned: set[str] = set()


class CredentialFileError(RuntimeError):
    """The credentials file exists but cannot be parsed (message has no values)."""


def path() -> Path:
    p = os.environ.get(ENV_PATH, "").strip()
    return Path(p).expanduser() if p else DEFAULT_PATH


def _load() -> dict:
    global _cache, _cache_path
    p = path()
    if _cache is not None and _cache_path == p:
        return _cache
    data: dict = {}
    if p.is_file():
        mode = p.stat().st_mode
        if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO) and str(p) not in _warned:
            print(f"[paperkb] warning: {p} is accessible by group/others; run: chmod 600 {p}",
                  file=sys.stderr)
            _warned.add(str(p))
        try:
            with p.open("rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            # Report the position only. The parser message is not reused verbatim:
            # it is not guaranteed to exclude file content on every Python version.
            m = re.search(r"line (\d+), column (\d+)", str(e))
            where = f"line {m.group(1)}, column {m.group(2)}" if m else "unknown position"
            raise CredentialFileError(f"cannot parse {p}: {where}") from None
    _cache, _cache_path = data, p
    return data


def reset_cache() -> None:
    global _cache, _cache_path
    _cache, _cache_path = None, None


def _lookup(section: str, key: str) -> tuple[str, str]:
    """Return (value, origin). origin in {'file', 'env:<NAME>', 'legacy-file', ''}."""
    v = _load().get(section, {}).get(key)
    if isinstance(v, str) and v.strip():
        return v.strip(), "file"
    for name in _ENV_FALLBACK.get((section, key), ()):
        ev = os.environ.get(name, "").strip()
        if ev:
            return ev, f"env:{name}"
    lf = _LEGACY_FILES.get((section, key))
    if lf and lf.is_file():
        lv = lf.read_text().strip()
        if lv:
            return lv, "legacy-file"
    return "", ""


def get(section: str, key: str) -> str:
    return _lookup(section, key)[0]


def mineru_token() -> str:
    return get("mineru", "token")


def sciverse_api_key() -> str:
    return get("sciverse", "api_key")


def contact_email() -> str:
    return get("contact", "email")


def status() -> dict:
    """What is configured and where from — never the values themselves."""
    p = path()
    out = {"file": str(p), "file_exists": p.is_file(), "file_private": None, "entries": {}}
    if p.is_file() and os.name == "posix":
        out["file_private"] = not bool(p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    for sec, key in _ENV_FALLBACK:
        v, origin = _lookup(sec, key)
        out["entries"][f"{sec}.{key}"] = {"configured": bool(v), "origin": origin or None}
    return out
