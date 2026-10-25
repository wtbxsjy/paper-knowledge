"""MinerU cloud API client (mineru.net, precision API /api/v4), stdlib only.

Ported from ``mineru_extract.py``'s ``MinerUClient`` (which used ``requests``)
with the same protocol:

1. ``POST /api/v4/file-urls/batch`` -> batch_id + presigned OSS upload URL
2. ``PUT`` the PDF to the presigned URL — **without** the MinerU bearer token;
   retried once with ``Content-Type: application/pdf`` (some URLs need it)
3. poll ``GET /api/v4/extract-results/batch/<id>`` until done / failed
4. download ``full_zip_url`` and extract it safely (no path traversal)

Privacy: this uploads the PDF to MinerU's cloud. Callers must have told the
user so before calling ``parse_pdf``.

Hosts contacted (all must be allowlisted where egress is filtered):
``mineru.net`` (API), ``mineru.oss-cn-shanghai.aliyuncs.com`` (upload),
``cdn-mineru.openxlab.org.cn`` (result ZIP) — observed 2026-09.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from . import credentials, http

API_BASE = os.environ.get("MINERU_API_BASE", "https://mineru.net")
DONE = ("done", "success", "completed")
FAILED = ("failed", "error")


class MinerUCloudError(RuntimeError):
    pass


class MinerUCloudClient:
    def __init__(self, token: Optional[str] = None, api_base: str = API_BASE, log=print):
        self.token = token if token is not None else credentials.mineru_token()
        if not self.token:
            raise MinerUCloudError(
                "MinerU token not configured: add [mineru] token to "
                f"{credentials.path()} (see credentials.example.toml)")
        self.api_base = api_base.rstrip("/")
        self.log = log

    # -- low level ---------------------------------------------------------
    def _api(self, method: str, endpoint: str, payload: Optional[dict] = None, timeout=60) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self.api_base}{endpoint}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", http.user_agent())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with http.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                d = json.loads(raw)
                msg = d.get("msg", d.get("message", ""))
            except Exception:
                msg = raw[:200].decode("utf-8", "replace")
            raise MinerUCloudError(f"MinerU API error (HTTP {e.code}): {msg}") from None
        try:
            return json.loads(body)
        except ValueError:
            return {"ok": True, "raw": body.decode("utf-8", "replace")}

    def _put_file(self, url: str, path: Path, content_type: Optional[str]) -> tuple[int, bytes]:
        # http.put_raw, not urlopen: the presigned URL must not get an implicit Content-Type
        try:
            return http.put_raw(url, path.read_bytes(), content_type)
        except OSError as e:
            return 599, str(e).encode()

    # -- protocol ----------------------------------------------------------
    def submit_file(self, file_path, language="en", enable_ocr=True,
                    enable_table=True, enable_formula=True) -> dict:
        p = Path(file_path)
        resp = self._api("POST", "/api/v4/file-urls/batch", {
            "language": language,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
            "files": [{"name": p.name, "size": p.stat().st_size,
                       "is_ocr": enable_ocr, "data_id": uuid.uuid4().hex}],
        })
        if resp.get("code") != 0:
            raise MinerUCloudError(f"Batch upload failed: {resp.get('msg')}")
        batch_id = resp["data"]["batch_id"]
        url = resp["data"]["file_urls"][0]
        if isinstance(url, dict):
            url = url.get("url") or url.get("upload_url")
        if not url:
            raise MinerUCloudError("No upload URL in MinerU response")
        self.log(f"  uploading to MinerU cloud ({p.stat().st_size / 1e6:.1f} MB)")
        status, head = self._put_file(url, p, None)
        if status >= 400 or b"<Error>" in head:
            status, head = self._put_file(url, p, "application/pdf")
        if status >= 400 or b"<Error>" in head:
            raise MinerUCloudError(f"upload failed (HTTP {status}): {head[:300].decode('utf-8', 'replace')}")
        return {"kind": "batch", "id": batch_id, "file_name": p.name}

    def get_status(self, task: dict) -> dict:
        resp = self._api("GET", f"/api/v4/extract-results/batch/{task['id']}")
        data = resp.get("data", {}) or {}
        results = data.get("extract_result") or data.get("extract_results") or data.get("results") or []
        item = next((c for c in results if c.get("file_name") == task.get("file_name")), None)
        if not item and results:
            item = results[0]
        if not item:
            return {"state": "pending"}
        item = dict(item)
        item.setdefault("state", data.get("state") or data.get("batch_status") or "unknown")
        return item

    def wait(self, task: dict, timeout=900, poll_interval=3) -> dict:
        t0, last = time.time(), None
        while True:
            if time.time() - t0 > timeout:
                raise TimeoutError(f"MinerU task {task['id']} timed out after {timeout}s")
            item = self.get_status(task)
            state = str(item.get("state", "unknown")).lower()
            if state != last:
                self.log(f"  [{state}] {time.time() - t0:.0f}s")
                last = state
            if state in DONE:
                return item
            if state in FAILED:
                raise MinerUCloudError(f"MinerU task failed: {item.get('err_msg', 'unknown error')}")
            time.sleep(poll_interval)

    def download_zip(self, url: str, dest: Path) -> Path:
        req = urllib.request.Request(url, headers={"User-Agent": http.user_agent()})
        with http.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        return dest

    # -- one call ------------------------------------------------------------
    def parse_pdf(self, pdf_path, work_dir, language="en", enable_ocr=True, timeout=900) -> Path:
        """Upload, wait, download and extract. Returns the extracted directory.

        ``work_dir`` is created; the result ZIP is kept as ``work_dir/result.zip``
        and its content extracted to ``work_dir/extracted/``.
        """
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        task = self.submit_file(pdf_path, language=language, enable_ocr=enable_ocr)
        item = self.wait(task, timeout=timeout)
        url = item.get("full_zip_url") or item.get("zip_url") or item.get("result_url")
        if not url:
            raise MinerUCloudError("MinerU finished but returned no result ZIP URL")
        z = self.download_zip(url, work / "result.zip")
        out = work / "extracted"
        if out.exists():
            shutil.rmtree(out)
        safe_extract(z, out)
        return out


def safe_extract(zip_path: Path, dest: Path) -> None:
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for m in zf.infolist():
            target = (dest / m.filename).resolve()
            if target != dest and dest not in target.parents:
                raise MinerUCloudError(f"unsafe path in ZIP: {m.filename}")
            zf.extract(m, dest)


def find_markdown(extracted: Path) -> Optional[Path]:
    """Same preference as the original client: full.md > paper.md > result.md > largest .md."""
    files = [p for p in Path(extracted).rglob("*.md") if p.is_file()]
    if not files:
        return None
    by_name = {p.name.lower(): p for p in files}
    for n in ("full.md", "paper.md", "result.md"):
        if n in by_name:
            return by_name[n]
    return max(files, key=lambda p: p.stat().st_size)
