"""connectors/complaints.py: the first real case (Order 1032, 2026-10-27, Velcro came off a soft book). The reply text and the
customer's e-mail reach the site only once the owner approved exactly that text."""
import hashlib, importlib.util, pathlib, tempfile, unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "connectors" / "cbase.py").exists())

def load():
    s = importlib.util.spec_from_file_location("t_complaints", ROOT / "connectors" / "complaints.py")
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    tmp = pathlib.Path(tempfile.mkdtemp())
    m.STATE = tmp / "complaints.json"  # never the real brain/
    m.ROOT = tmp
    (tmp / "handoffs" / "echo").mkdir(parents=True)
    return m, tmp

class FakeBook:
    def __init__(self, items): self.items = items
    def get(self, i): return self.items.get(i)

REPLY = "Kedves Vevő!\n\nKérünk, küldj pár fotót a tépőzárakról.\n\nBarni\nMonti Világ\n"
ORDERS = [{"name": "#1032", "email": "vevo@example.com"}, {"name": "#1031", "email": "masik@example.com"}]

def item(state, decision=None, text=REPLY, file="handoffs/echo/valasz.md"):
    return {"state": state, "decision": decision, "file": file, "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()}

class Complaints(unittest.TestCase):
    def setUp(self):
        self.m, self.tmp = load()
        (self.tmp / "handoffs" / "echo" / "valasz.md").write_text(REPLY, encoding="utf-8")
        self.m.record("2026-10-27-1032", order="#1032", received_at="2026-10-26T18:00:00+01:00",
                      products="1× Zsiráf Kalandkönyv, 1× Farmer Kalandkönyv", summary="Feljöttek a tépőzárak.",
                      subject="#1032 rendelésed", reply_file="handoffs/echo/valasz.md", approval="surp")

    def one(self, book): return self.m.payload(ORDERS, book)[0]

    def test_waiting_for_approval_sends_no_text_and_no_email(self):
        c = self.one(FakeBook({"surp": item("pending")}))
        self.assertEqual(c["status"], "reply_waiting_approval")
        self.assertIsNone(c["reply_body"])
        self.assertNotIn("reply_to_email", c)

    def test_approved_sends_the_text_and_the_orders_email(self):
        c = self.one(FakeBook({"surp": item("decided", "ok")}))
        self.assertEqual(c["status"], "reply_ready")
        self.assertEqual(c["reply_body"], REPLY)
        self.assertEqual(c["reply_to_email"], "vevo@example.com")  # the order's own, not another order's

    def test_text_changed_after_approval_is_not_sent(self):
        (self.tmp / "handoffs" / "echo" / "valasz.md").write_text(REPLY + "Csere is jár.\n", encoding="utf-8")
        c = self.one(FakeBook({"surp": item("consumed", "ok")}))
        self.assertEqual(c["status"], "reply_ready")
        self.assertNotIn("reply_body", c)  # missing key: the site keeps the approved text it has, never the changed one

    def test_sent_back_or_dropped_is_new_and_clears_the_text(self):
        for d in ("edit", "drop"):
            c = self.one(FakeBook({"surp": item("decided", d)}))
            self.assertEqual(c["status"], "new"); self.assertIsNone(c["reply_body"])

    def test_echos_status_wins_and_order_outside_window_keeps_email(self):
        self.m.record("2026-10-27-1032", status="exchange_in_progress")
        c = self.m.payload([], FakeBook({"surp": item("consumed", "ok")}))[0]
        self.assertEqual(c["status"], "exchange_in_progress")
        self.assertNotIn("reply_to_email", c)

    def test_approval_gone_from_queue_keeps_site_state(self):
        c = self.one(FakeBook({}))
        self.assertNotIn("status", c); self.assertNotIn("reply_body", c)

    def test_show_never_prints_text_or_email(self):
        self.m._book = lambda: FakeBook({"surp": item("decided", "ok")})
        out = str(self.m.show())
        self.assertNotIn("vevo@example.com", out); self.assertNotIn("tépőzárakról", out)

    def test_refuses_bad_input(self):
        with self.assertRaises(self.m.ConnectorError): self.m.record("x", status="reply_ready")  # follows the approval, not set by hand
        with self.assertRaises(self.m.ConnectorError): self.m.record("x", reply_file="brain/approvals.json")
        with self.assertRaises(self.m.ConnectorError): self.m.record("x", received_at="tegnap")

    def test_no_complaints_is_an_empty_list(self):
        self.m.STATE.unlink()
        self.assertEqual(self.m.payload(ORDERS, FakeBook({})), [])

if __name__ == "__main__": unittest.main()
