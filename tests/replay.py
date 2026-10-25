"""Replay transport: serve recorded HTTP exchanges instead of the network.

Records are JSON files ``{url, method, body, status, headers, response}`` as
written by the private repo's ``tests/baseline/record_baseline.py``.  Matching
ignores the ``tool`` / ``email`` query parameters (caller identification,
which legitimately changed with the rename) and compares JSON bodies
structurally.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path

IGNORED_PARAMS = {"tool", "email"}


def _norm_url(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k not in IGNORED_PARAMS]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(sorted(q)), ""))


def _key(method: str, url: str, body) -> str:
    return f"{method} {_norm_url(url)} {json.dumps(body, sort_keys=True, ensure_ascii=False) if body is not None else ''}"


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, status: int, headers: dict):
        super().__init__(data)
        self.status = status
        self.headers = Message()
        for k, v in (headers or {}).items():
            if v is not None:
                self.headers[k] = v

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Replay:
    def __init__(self, http_dir: Path):
        self.table: dict[str, dict] = {}
        for p in sorted(Path(http_dir).glob("*.json")):
            r = json.loads(p.read_text(encoding="utf-8"))
            self.table[_key(r["method"], r["url"], r.get("body"))] = r
        self.misses: list[str] = []
        self.hits = 0

    def __call__(self, req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else req
        data = getattr(req, "data", None)
        body = json.loads(data) if data else None
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        k = _key(method, url, body)
        r = self.table.get(k)
        if r is None:
            self.misses.append(k[:200])
            raise urllib.error.URLError("replay miss")
        self.hits += 1
        payload = r["response"].encode("utf-8")
        if r["status"] >= 400:
            h = Message()
            for hk, hv in (r.get("headers") or {}).items():
                if hv is not None:
                    h[hk] = hv
            raise urllib.error.HTTPError(url, r["status"], "replayed", h, io.BytesIO(payload))
        return _Resp(payload, r["status"], r.get("headers") or {})
