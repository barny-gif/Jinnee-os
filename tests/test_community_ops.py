"""Community feed reader: GET only, bearer header, one-time attest key never comes out, errors name the problem."""
import os, sys, pathlib, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
import importlib.util
_spec = importlib.util.spec_from_file_location("community_ops", ROOT / "connectors" / "community" / "ops.py")
cm = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(cm)
from cbase import ConnectorError

BASE = "https://api.example.com/agent/v1"
ENV = {"COMMUNITY_API_BASE": BASE + "/", "COMMUNITY_API_KEY": "key_test000"}


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.ok, self.text = 200 <= status < 300, str(data)
    def json(self): return self._data


@mock.patch.dict(os.environ, ENV)
class Reader(unittest.TestCase):
    def test_search_sends_bearer_and_query(self):
        with mock.patch.object(cm.requests, "get", return_value=Resp(data={"items": []})) as g:
            self.assertEqual(cm.search("webshop", limit="5"), {"items": []})
        url, kw = g.call_args[0][0], g.call_args[1]
        self.assertEqual(url, BASE + "/search")  # trailing slash of the setting does not double
        self.assertEqual(kw["headers"]["Authorization"], "Bearer key_test000")
        self.assertEqual(kw["params"], {"q": "webshop", "limit": 5})

    def test_attest_key_is_dropped(self):
        data = {"items": [], "attest_grant": {"attest_key": "secret-attest-123"}}
        with mock.patch.object(cm.requests, "get", return_value=Resp(data=data)):
            out = cm.feed()
        self.assertNotIn("attest_grant", out)
        self.assertTrue(out["attest_key_offered"])
        self.assertNotIn("secret-attest-123", str(out))

    def test_no_write_op_exists(self):
        self.assertEqual(sorted(cm.OPS), ["feed", "highlights", "mentions", "post", "search"])
        src = (ROOT / "connectors" / "community" / "ops.py").read_text(encoding="utf-8")
        for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete", "requests.request"):
            self.assertNotIn(verb, src)

    def test_refused_key_and_bad_input(self):
        with mock.patch.object(cm.requests, "get", return_value=Resp(401, {})):
            with self.assertRaisesRegex(ConnectorError, "refused"): cm.mentions()
        with mock.patch.object(cm.requests, "get") as g:
            with self.assertRaises(ConnectorError): cm.search("")
            with self.assertRaises(ConnectorError): cm.feed(limit="99")
            g.assert_not_called()  # validated before any call

    def test_missing_settings(self):
        for name in ENV:
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: ""}):
                with self.assertRaisesRegex(ConnectorError, name): cm.highlights()


if __name__ == "__main__":
    unittest.main()
