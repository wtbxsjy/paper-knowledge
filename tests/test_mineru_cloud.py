"""Offline protocol test for the stdlib MinerU cloud client (fake transport)."""
import io, json, sys, tempfile, unittest, urllib.error, zipfile
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from paperkb_core import http, mineru_cloud  # noqa: E402

TOKEN = "tok-never-sent-to-oss"


class R(io.BytesIO):
    def __init__(self, b, status=200):
        super().__init__(b); self.status = status; self.headers = Message()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def make_zip(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return b.getvalue()


class Fake:
    def __init__(self, zip_bytes, first_put_status=403, polls=2):
        self.calls, self.zip, self.first_put_status, self.polls = [], zip_bytes, first_put_status, polls

    def __call__(self, req, timeout=None):
        url, m = req.full_url, req.get_method()
        auth = req.get_header("Authorization")
        self.calls.append((m, url, auth, req.get_header("Content-type")))
        if url.endswith("/api/v4/file-urls/batch"):
            body = json.loads(req.data)
            assert body["files"][0]["name"] == "p.pdf"
            return R(json.dumps({"code": 0, "data": {"batch_id": "B1", "file_urls": ["https://oss.example/up?sig=1"]}}).encode())
        if "/extract-results/batch/B1" in url:
            self.polls -= 1
            state = "running" if self.polls > 0 else "done"
            item = {"file_name": "p.pdf", "state": state}
            if state == "done":
                item["full_zip_url"] = "https://cdn.example/r.zip"
            return R(json.dumps({"code": 0, "data": {"extract_result": [item]}}).encode())
        if url == "https://cdn.example/r.zip":
            return R(self.zip)
        raise AssertionError(url)


    def put(self, url, data, content_type=None, timeout=None):
        self.calls.append(("PUT", url, None, content_type))
        assert url.startswith("https://oss.example/up"), url
        put_n = sum(1 for c in self.calls if c[0] == "PUT")
        if put_n == 1 and self.first_put_status >= 400:
            return self.first_put_status, b"<Error>sig</Error>"
        return 200, b""


class Cloud(unittest.TestCase):
    def setUp(self):
        self._orig = (http.urlopen, http.put_raw)

    def tearDown(self):
        http.urlopen, http.put_raw = self._orig

    def test_full_flow_and_token_scope(self):
        fake = Fake(make_zip({"full.md": "# t\n![](images/a.jpg)", "images/a.jpg": "x", "x_content_list.json": "[]"}))
        http.urlopen = fake
        http.put_raw = fake.put
        with tempfile.TemporaryDirectory() as t:
            pdf = Path(t) / "p.pdf"; pdf.write_bytes(b"%PDF-1.4")
            c = mineru_cloud.MinerUCloudClient(token=TOKEN, log=lambda *_: None)
            mineru_cloud.time.sleep = lambda *_: None
            out = c.parse_pdf(pdf, Path(t) / "work")
            self.assertEqual(mineru_cloud.find_markdown(out).name, "full.md")
            self.assertTrue((out / "images" / "a.jpg").is_file())
        puts = [c for c in fake.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 2, "retry with explicit content type after first failure")
        self.assertIsNone(puts[0][3], "first PUT must carry no Content-Type (presigned URL)")
        self.assertEqual(puts[1][3], "application/pdf")
        for m, url, auth, _ in fake.calls:
            if "mineru.net" in url:
                self.assertEqual(auth, f"Bearer {TOKEN}")
            else:
                self.assertIsNone(auth, f"token must not be sent to {url}")

    def test_zip_slip_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            z = Path(t) / "bad.zip"; z.write_bytes(make_zip({"../evil.txt": "x"}))
            with self.assertRaises(mineru_cloud.MinerUCloudError):
                mineru_cloud.safe_extract(z, Path(t) / "out")

    def test_missing_token_message(self):
        import os
        from paperkb_core import credentials
        env = {k: os.environ.pop(k) for k in ("MINERU_TOKEN",) if k in os.environ}
        os.environ["PAPERKB_CREDENTIALS"] = "/nonexistent/c.toml"; credentials.reset_cache()
        orig = credentials._LEGACY_FILES.copy(); credentials._LEGACY_FILES.clear()
        try:
            with self.assertRaises(mineru_cloud.MinerUCloudError) as cm:
                mineru_cloud.MinerUCloudClient()
            self.assertIn("[mineru] token", str(cm.exception))
        finally:
            credentials._LEGACY_FILES.update(orig); os.environ.pop("PAPERKB_CREDENTIALS"); os.environ.update(env)
            credentials.reset_cache()


if __name__ == "__main__":
    unittest.main()


class PutRawHeaders(unittest.TestCase):
    """Real socket: put_raw must not add Content-Type (the bug urllib caused)."""
    def test_no_implicit_content_type(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_PUT(self):
                seen.update(self.headers.items()); self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200); self.end_headers()
        srv = HTTPServer(("127.0.0.1", 0), H)
        th = threading.Thread(target=srv.handle_request); th.start()
        st, _ = http.put_raw(f"http://127.0.0.1:{srv.server_port}/up?sig=1", b"%PDF")
        th.join(); srv.server_close()
        self.assertEqual(st, 200)
        self.assertNotIn("Content-Type", seen)
        # explicit type is still sent
        srv = HTTPServer(("127.0.0.1", 0), H); seen.clear()
        th = threading.Thread(target=srv.handle_request); th.start()
        http.put_raw(f"http://127.0.0.1:{srv.server_port}/up", b"%PDF", "application/pdf")
        th.join(); srv.server_close()
        self.assertEqual(seen.get("Content-Type"), "application/pdf")
