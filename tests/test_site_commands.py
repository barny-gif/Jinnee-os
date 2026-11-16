"""Site commands: only exchange labels are carried out, only when switched on and not unattended; others stay open."""
import os, sys, pathlib, unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
import site_commands as sc
from cbase import ConnectorError

ENV = {"ECOM_SITE_URL": "https://example-shop.hu", "ECOM_INGEST_SECRET": "s3cret", "ECOM_SITE_COMMANDS": "on", "JINNEE_UNATTENDED": ""}
EXCHANGE = {"id": "11111111-1111-1111-1111-111111111111", "order_id": "5042", "order_name": "#1001", "action": "label",
            "params": {"type": "exchange", "send_items": "1× Wooden puzzle", "back_items": "1× Puzzle"}}
OTHER = {"id": "22222222-2222-2222-2222-222222222222", "order_id": "5043", "action": "invoice", "params": None}


class Resp:
    def __init__(self, status=200, data=None): self.status_code, self._d, self.ok = status, data, 200 <= status < 300
    def json(self): return self._d


@mock.patch.dict(os.environ, ENV)
class SiteCommands(unittest.TestCase):
    def setUp(self):
        self.get = mock.patch.object(sc.requests, "get", return_value=Resp(data={"ok": True, "commands": [EXCHANGE, OTHER]})).start()
        self.post = mock.patch.object(sc.requests, "post", return_value=Resp(data={"ok": True, "consumed": 1})).start()
        self.addCleanup(mock.patch.stopall)

    def test_exchange_runs_and_only_it_is_reported(self):
        prep = mock.Mock(return_value={"order": "#1001", "uploaded": [{"parcel_id": 1}, {"parcel_id": 2}], "warnings": []})
        out = sc.run(send="yes", prepare=prep)
        prep.assert_called_once_with("5042", "1× Wooden puzzle", "1× Puzzle", send="yes", again="")
        sent = self.post.call_args[1]["json"]["results"]
        self.assertEqual([r["id"] for r in sent], [EXCHANGE["id"]])  # the invoice command stays open
        self.assertTrue(sent[0]["ok"]); self.assertIn("1, 2", sent[0]["result"])
        self.assertEqual(self.post.call_args[1]["headers"], {"x-ecom-secret": "s3cret"})
        self.assertEqual(out["consumed"], 1)

    def test_refusal_is_written_back_as_not_ok(self):
        prep = mock.Mock(side_effect=ConnectorError("[exchange] #1001: no house number in the shipping address: nothing uploaded"))
        sc.run(send="yes", prepare=prep)
        r = self.post.call_args[1]["json"]["results"][0]
        self.assertFalse(r["ok"]); self.assertIn("house number", r["result"])

    def test_nothing_runs_dry_off_or_unattended(self):
        prep = mock.Mock()
        self.assertEqual(sc.run(send="no", prepare=prep)["note"], "dry run")
        with mock.patch.dict(os.environ, {"ECOM_SITE_COMMANDS": ""}):
            self.assertIn("off", sc.run(send="yes", prepare=prep)["note"])
        with mock.patch.dict(os.environ, {"JINNEE_UNATTENDED": "1"}):
            self.assertIn("scheduled", sc.run(send="yes", prepare=prep)["note"])
        prep.assert_not_called(); self.post.assert_not_called()

    def test_plain_http_site_is_refused(self):
        with mock.patch.dict(os.environ, {"ECOM_SITE_URL": "http://example-shop.hu"}):
            with self.assertRaises(ConnectorError): sc.fetch()


if __name__ == "__main__":
    unittest.main()
