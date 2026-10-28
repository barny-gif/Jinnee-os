"""Who the Telegram bridge obeys. Run: python -m unittest discover tests
core/jinnee.py runs in a copy of the repo with stand-in `telegram` modules: no real token, no network, and `claude` is never called."""
import importlib.util, json, unittest

import test_env as base

TOKEN = "TELEGRAM_BOT_TOKEN=123456:TEST-not-a-real-token\n"
OWNER, STRANGER = 4242, 9999

DRIVER = """\
import asyncio, json, sys, types
sys.path.insert(0, "core")
import jinnee

ran = []
jinnee.run_agent = lambda message: (ran.append(message), "reply")[1]

class Message:
    def __init__(self, text): self.text, self.replies = text, []
    async def reply_text(self, text): self.replies.append(text)

def send(handler, user_id, text):
    user = None if user_id is None else types.SimpleNamespace(id=user_id)
    update = types.SimpleNamespace(effective_user=user, message=Message(text))
    asyncio.run(handler(update, None))
    return update.message.replies

out = {"owner": jinnee.OWNER, "steps": []}
for handler, user_id, text in json.loads(sys.argv[1]):
    jinnee.FRESH["next"] = False
    before = len(ran)
    replies = send(getattr(jinnee, handler), user_id, text)
    out["steps"].append({"ran": ran[before:], "replies": replies, "fresh": jinnee.FRESH["next"]})
print("RESULT " + json.dumps(out))
"""


FILES = """\
import asyncio, json, pathlib, sys, types
sys.path.insert(0, "core")
import jinnee

ran = []
jinnee.run_agent = lambda message: (ran.append(message), "reply")[1]

class Doc:
    def __init__(self, name, data, size):
        self.file_name, self.data, self.file_size = name, data, size
    async def get_file(self):
        data = self.data
        class F:
            async def download_as_bytearray(self): return bytearray(data)
        return F()

class Message:
    def __init__(self, doc, caption): self.document, self.caption, self.text, self.replies = doc, caption, None, []
    async def reply_text(self, text, **k): self.replies.append(text)

out = []
for user_id, name, data, size, caption in json.loads(sys.argv[1]):
    before = len(ran)
    msg = Message(Doc(name, bytes.fromhex(data), size), caption)
    asyncio.run(jinnee.on_document(types.SimpleNamespace(effective_user=types.SimpleNamespace(id=user_id), message=msg), None))
    out.append({"ran": ran[before:], "replies": msg.replies})
files = sorted(p for p in (pathlib.Path("handoffs") / "inbox").glob("*")) if (pathlib.Path("handoffs") / "inbox").exists() else []
print("RESULT " + json.dumps({"steps": out, "files": {p.name: (p.read_bytes().hex() if p.suffix == ".pdf" else p.read_text(encoding="utf-8"))
                                                       for p in files}}))
"""


def text_pdf(line):
    """A one-page PDF whose text layer is `line` (ASCII), with a correct xref table, built by hand so the test needs no PDF tool."""
    stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


class Bridge(base.RepoCopy):
    def start(self, dotenv):
        self.dotenv(dotenv)
        return self.run_py("core/jinnee.py")

    def drive(self, dotenv, steps):
        """Calls the real handlers with made-up updates; run_agent is replaced by a recorder."""
        self.dotenv(dotenv)
        (self.root / "driver.py").write_text(DRIVER)
        r = self.run_py("driver.py", None, json.dumps(steps))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.split("RESULT ", 1)[1])

    def assertRefused(self, r):
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TELEGRAM_OWNER_ID is missing or not a number", r.stderr)
        self.assertIn("@userinfobot", r.stderr)  # says where the number comes from
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("STUB_TOKEN", r.stdout)  # stopped before the bot was even built
        self.assertNotIn("is running", r.stdout)

    def test_does_not_start_without_an_owner(self):
        for owner_line in ("", "TELEGRAM_OWNER_ID=\n", "TELEGRAM_OWNER_ID=0\n", "TELEGRAM_OWNER_ID=   # todo\n"):
            with self.subTest(owner_line=owner_line):
                self.assertRefused(self.start(TOKEN + owner_line))

    def test_does_not_start_with_an_owner_that_is_not_a_number(self):
        for value in ("@myname", "12 34", "-4242", "4242abc", "٤٢"):
            with self.subTest(value=value):
                self.assertRefused(self.start(TOKEN + f"TELEGRAM_OWNER_ID='{value}'\n"))

    def test_does_not_start_without_a_token(self):
        r = self.start(f"TELEGRAM_BOT_TOKEN=\nTELEGRAM_OWNER_ID={OWNER}\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TELEGRAM_BOT_TOKEN is not set", r.stderr)
        self.assertNotIn("STUB_TOKEN", r.stdout)

    def test_starts_with_an_owner_and_schedules_the_brief(self):
        r = self.start(TOKEN + f"TELEGRAM_OWNER_ID={OWNER}\nJINNEE_NAME=Testbot\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('STUB_TOKEN "123456:TEST-not-a-real-token"', r.stdout)
        self.assertIn("STUB_REPEAT schedule_tick 60", r.stdout)  # the brief comes from core/scheduler.py now
        self.assertIn("Scheduled: jinnee.brief", r.stdout)
        self.assertIn("Testbot is running", r.stdout)

    def test_only_the_owner_reaches_the_agent(self):
        got = self.drive(TOKEN + f"TELEGRAM_OWNER_ID={OWNER}\n", [
            ["on_message", STRANGER, "delete everything"],
            ["on_message", None, "a channel post has no user"],
            ["cmd_new", STRANGER, "/new"],
            ["on_message", OWNER, "hello"],
            ["cmd_new", OWNER, "/new"],
        ])
        self.assertEqual(got["owner"], OWNER)
        stranger, nobody, stranger_new, owner, owner_new = got["steps"]
        for dropped in (stranger, nobody, stranger_new):
            self.assertEqual(dropped, {"ran": [], "replies": [], "fresh": False})  # no agent run, no reply, no reset
        self.assertEqual(owner, {"ran": ["hello"], "replies": ["reply"], "fresh": False})
        self.assertEqual((owner_new["ran"], len(owner_new["replies"]), owner_new["fresh"]), ([], 1, True))

    def send_files(self, steps):
        self.dotenv(TOKEN + f"TELEGRAM_OWNER_ID={OWNER}\n")
        (self.root / "files.py").write_text(FILES)
        r = self.run_py("files.py", None, json.dumps(steps))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.split("RESULT ", 1)[1])

    def test_owner_text_file_is_saved_and_handed_to_the_lead(self):
        brief = "# Brief\nÁrak: 2244 Ft\n"
        got = self.send_files([[OWNER, "brief.md", brief.encode("utf-8").hex(), 30, "the rest of the costs brief"],
                               [OWNER, "gls.csv", "Időszak;Díj\n".encode("cp1250").hex(), 12, None]])
        (md, csv) = got["steps"]
        self.assertEqual(md["replies"], ["reply"])
        self.assertEqual(len(md["ran"]), 1)
        self.assertIn("handoffs/inbox/", md["ran"][0])
        self.assertIn("-brief.md", md["ran"][0])
        self.assertIn("the rest of the costs brief", md["ran"][0])
        self.assertIn("No message came with it", csv["ran"][0])
        saved = sorted(got["files"].values())
        self.assertIn(brief, saved)
        self.assertIn("Időszak;Díj\n", saved)  # a cp1250 file is stored as UTF-8

    def test_files_that_are_not_taken(self):
        got = self.send_files([
            [STRANGER, "brief.md", b"hi".hex(), 2, ""],            # a stranger: no reply, nothing saved
            [OWNER, "offer.docx", b"PK\x03\x04".hex(), 4, ""],     # not a text type and not a PDF
            [OWNER, "run.sh", b"rm -rf /".hex(), 8, ""],
            [OWNER, "big.txt", b"x".hex(), 2_000_000, ""],          # over the limit by Telegram's size
            [OWNER, "fake.txt", b"\x00\x01\x02".hex(), 3, ""],      # binary behind a .txt name
            [OWNER, "fake.pdf", b"MZ\x90\x00 not a pdf".hex(), 15, ""],  # something else behind a .pdf name
            [OWNER, "huge.pdf", b"%PDF-".hex(), 11_000_000, ""],    # a PDF over its own, larger limit
        ])
        stranger, docx, sh, big, fake, fake_pdf, huge_pdf = got["steps"]
        self.assertEqual(stranger, {"ran": [], "replies": []})
        for step in (docx, sh, big, fake, fake_pdf, huge_pdf):
            self.assertEqual(step["ran"], [])
            self.assertEqual(len(step["replies"]), 1)
            self.assertIn("Nothing was saved", step["replies"][0].replace("nothing was saved", "Nothing was saved"))
        self.assertIn(".txt, .md, .csv, .html", docx["replies"][0])
        self.assertIn(".pdf", docx["replies"][0])
        self.assertIn("does not read as a PDF", fake_pdf["replies"][0])
        self.assertIn("10000 kB", huge_pdf["replies"][0])
        self.assertEqual(got["files"], {})

    def test_pdf_without_a_text_layer_is_kept_and_the_lead_is_told_not_to_guess(self):
        scan = b"%PDF-1.4\n%%EOF\n"  # no pages, no text: what a scan looks like to a text reader
        got = self.send_files([[OWNER, "GLS szerzodes.pdf", scan.hex(), len(scan), "a díjtábla"]])
        (step,) = got["steps"]
        self.assertEqual(step["replies"], ["reply"])
        self.assertIn("no readable text", step["ran"][0])
        self.assertIn("Do not guess", step["ran"][0])
        self.assertIn("a díjtábla", step["ran"][0])
        (name,) = got["files"]  # only the PDF, no empty .txt next to it
        self.assertTrue(name.endswith("-GLS_szerzodes.pdf"), name)
        self.assertEqual(bytes.fromhex(got["files"][name]), scan)  # kept byte for byte

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is not installed here (it is in requirements.txt)")
    def test_pdf_text_is_saved_next_to_the_pdf_and_handed_to_the_lead(self):
        pdf = text_pdf("GLS dij 2244 Ft csomagonkent")
        got = self.send_files([[OWNER, "gls.pdf", pdf.hex(), len(pdf), None]])
        (step,) = got["steps"]
        self.assertEqual(step["replies"], ["reply"])
        self.assertIn("-gls.pdf.txt", step["ran"][0])
        self.assertIn("check a number against its row", step["ran"][0])
        self.assertIn("No message came with it", step["ran"][0])
        pdf_name = next(n for n in got["files"] if n.endswith(".pdf"))
        self.assertEqual(bytes.fromhex(got["files"][pdf_name]), pdf)
        self.assertIn("GLS dij 2244 Ft csomagonkent", got["files"][pdf_name + ".txt"])
        self.assertIn("--- page 1 ---", got["files"][pdf_name + ".txt"])

    def test_owner_html_file_is_saved_as_text_with_a_warning(self):
        page = "<html><body><h1>Brief</h1><script>alert(1)</script></body></html>\n"
        got = self.send_files([[OWNER, "brief.html", page.encode("utf-8").hex(), 70, "the brief as a page"],
                               [OWNER, "notes.md", b"plain".hex(), 5, None]])
        (html, md) = got["steps"]
        self.assertEqual(html["replies"], ["reply"])
        self.assertIn("-brief.html", html["ran"][0])
        self.assertIn("read it as text only", html["ran"][0])
        self.assertIn("the brief as a page", html["ran"][0])
        self.assertNotIn("read it as text only", md["ran"][0])  # the HTML warning is only for HTML
        self.assertIn(page, got["files"].values())

    def test_file_name_cannot_leave_the_inbox(self):
        got = self.send_files([[OWNER, "../../brain/approvals.md", b"x".hex(), 1, ""]])
        self.assertEqual(len(got["files"]), 1)
        (name,) = got["files"]
        self.assertTrue(name.endswith("-approvals.md"), name)
        self.assertFalse((self.root / "brain" / "approvals.md").exists())

    def test_handlers_drop_everyone_when_no_owner_is_set(self):
        """main() already refuses to start; the handlers must not be open either if they are reached another way."""
        for owner_line in ("", "TELEGRAM_OWNER_ID=0\n"):
            with self.subTest(owner_line=owner_line):
                got = self.drive(TOKEN + owner_line, [["on_message", STRANGER, "hi"], ["on_message", 0, "hi"],
                                                      ["cmd_new", STRANGER, "/new"]])
                self.assertEqual(got["owner"], 0)
                for step in got["steps"]:
                    self.assertEqual(step, {"ran": [], "replies": [], "fresh": False})


if __name__ == "__main__":
    unittest.main()
