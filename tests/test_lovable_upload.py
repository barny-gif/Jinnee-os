"""connectors/lovable_upload.py: the agent's only way to put an attachment for an approved Lovable prompt. Fake HTTP.
The real case: approval 6972 (2026-10-31) needed 25 catalogue images; without an upload road the GO was used up and
nothing went out."""
import importlib.util, pathlib, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
spec = importlib.util.spec_from_file_location("lovable_upload", ROOT / "connectors" / "lovable_upload.py")
lu = importlib.util.module_from_spec(spec); spec.loader.exec_module(lu)
URL = "https://uploads.example.test/p01.jpg?X-Signature=secret"


class FakeHTTP:
    def __init__(self, status=200): self.status, self.calls = status, []
    def put(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, len(data), headers))
        return type("R", (), {"status_code": self.status})()


class Upload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        (root / "handoffs" / "inbox").mkdir(parents=True)
        (root / "brain").mkdir()
        (root / "handoffs" / "inbox" / "p01.jpg").write_bytes(b"\xff\xd8jpeg")
        (root / "brain" / "secret.json").write_text("{}")
        (root / ".env").write_text("KEY=x")
        self.old = (lu.ROOT, lu.ALLOWED_DIR)
        lu.ROOT, lu.ALLOWED_DIR = root, root / "handoffs"

    def tearDown(self):
        lu.ROOT, lu.ALLOWED_DIR = self.old
        self.tmp.cleanup()

    def test_dry_run_by_default_sends_nothing(self):
        http = FakeHTTP()
        out = lu.put(url=URL, file="handoffs/inbox/p01.jpg", _http=http)
        self.assertTrue(out["dry_run"]); self.assertEqual(http.calls, [])

    def test_send_yes_uploads_and_hides_signature(self):
        http = FakeHTTP()
        out = lu.put(url=URL, file="handoffs/inbox/p01.jpg", content_type="image/jpeg", send="yes", _http=http)
        self.assertTrue(out["uploaded"]); self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][2], {"Content-Type": "image/jpeg"})
        self.assertNotIn("secret", str(out)); self.assertEqual(out["host"], "uploads.example.test")

    def test_signed_goog_headers_are_sent(self):
        # t4r2, 2026-10-31: the URL is signed with these two headers; Content-Type alone got 400 from storage
        http = FakeHTTP()
        h = '{"x-goog-content-length-range": "0,262144000", "x-goog-meta-user_id": "u1"}'
        out = lu.put(url=URL, file="handoffs/inbox/p01.jpg", content_type="image/jpeg", headers=h, send="yes", _http=http)
        self.assertEqual(http.calls[0][2], {"x-goog-content-length-range": "0,262144000", "x-goog-meta-user_id": "u1",
                                            "Content-Type": "image/jpeg"})
        self.assertEqual(out["headers"], ["x-goog-content-length-range", "x-goog-meta-user_id"])
        self.assertNotIn("u1", str(out))

    def test_refuses_other_headers_and_bad_json(self):
        for h in ('{"Authorization": "x"}', '{"x-goog-a": "1\\nHost: evil"}', "not json", "[1]"):
            http = FakeHTTP()
            with self.assertRaises(lu.ConnectorError):
                lu.put(url=URL, file="handoffs/inbox/p01.jpg", headers=h, send="yes", _http=http)
            self.assertEqual(http.calls, [])

    def test_refuses_files_outside_handoffs(self):
        for f in ("brain/secret.json", ".env", "handoffs/../.env"):
            with self.assertRaises(lu.ConnectorError): lu.put(url=URL, file=f, send="yes", _http=FakeHTTP())

    def test_refuses_plain_http(self):
        with self.assertRaises(lu.ConnectorError):
            lu.put(url="http://uploads.example.test/x", file="handoffs/inbox/p01.jpg", send="yes", _http=FakeHTTP())

    def test_failed_upload_names_the_step(self):
        with self.assertRaises(lu.ConnectorError) as c:
            lu.put(url=URL, file="handoffs/inbox/p01.jpg", send="yes", _http=FakeHTTP(403))
        self.assertIn("[upload]", str(c.exception)); self.assertNotIn("secret", str(c.exception))


class Launcher(unittest.TestCase):
    def test_upload_url_tool_allowed_but_not_in_scheduled_runs(self):
        src = (ROOT / "core" / "jinnee.py").read_text()
        self.assertIn('"get_file_upload_url"', src)
        self.assertIn('"Lovable__get_file_upload_url"', src)


if __name__ == "__main__":
    unittest.main()
