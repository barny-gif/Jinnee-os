"""Photos from the owner (step 6): a Telegram photo or an image sent as a file is checked by its first bytes, saved under
handoffs/inbox/, and the lead is told to open it with Read; the caption is the message. Run: python -m unittest discover tests
The images are generated here (a tiny plain PNG, made-up bytes): no real photo, no customer data."""
import json, struct, unittest, zlib

import test_env as base
from test_bridge import TOKEN, OWNER, STRANGER

PHOTOS = """\
import asyncio, json, logging, pathlib, sys, types
sys.path.insert(0, "core")
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
import jinnee

ran = []
jinnee.run_agent = lambda message, *a, **k: (ran.append(message), "reply")[1]

class Media:
    def __init__(self, data, size, name=None, mime=None, broken=False):
        self.data, self.file_size, self.file_name, self.mime_type, self.broken, self.asked = data, size, name, mime, broken, 0
    async def get_file(self):
        self.asked += 1
        if self.broken: raise ConnectionError("network down")
        data = self.data
        class F:
            async def download_as_bytearray(self): return bytearray(data)
        return F()

class Message:
    def __init__(self, caption): self.caption, self.text, self.photo, self.document, self.replies = caption, None, (), None, []
    async def reply_text(self, text, **k): self.replies.append(text)

out = []
for user_id, kind, name, data, size, caption, mime, broken in json.loads(pathlib.Path(sys.argv[1]).read_text()):
    before = len(ran)
    msg = Message(caption)
    media = Media(bytes.fromhex(data), size, name, mime, broken)
    if kind == "photo": msg.photo = (Media(b"small", 10), media)  # Telegram lists the sizes smallest first
    else: msg.document = media
    handler = jinnee.on_photo if kind == "photo" else jinnee.on_document
    asyncio.run(handler(types.SimpleNamespace(effective_user=types.SimpleNamespace(id=user_id), message=msg), None))
    out.append({"ran": ran[before:], "replies": msg.replies, "downloaded": media.asked})
inbox = pathlib.Path("handoffs") / "inbox"
files = sorted(inbox.glob("*")) if inbox.exists() else []
print("RESULT " + json.dumps({"steps": out, "files": {p.name: p.read_bytes().hex() for p in files}}))
"""


def png(width=2, height=2, rgb=(200, 30, 30)):
    """A real, tiny, single-colour PNG, built with the standard library."""
    def chunk(kind, body): return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64 + b"\xff\xd9"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 28


class Photos(base.RepoCopy):
    def send(self, steps):
        """steps: [user, "photo"|"doc", name, bytes, size, caption, mime, broken]"""
        self.dotenv(TOKEN + f"TELEGRAM_OWNER_ID={OWNER}\n")
        (self.root / "photos.py").write_text(PHOTOS)
        rows = [[u, k, n, d.hex(), s if s is not None else len(d), c, m, b] for u, k, n, d, s, c, m, b in steps]
        (self.root / "steps.json").write_text(json.dumps(rows))  # a file: a 10 MB image does not fit on a command line
        r = self.run_py("photos.py", None, "steps.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stderr = r.stderr
        return json.loads(r.stdout.split("RESULT ", 1)[1])

    def test_a_photo_with_a_caption_is_saved_and_the_caption_is_the_message(self):
        got = self.send([[OWNER, "photo", None, JPEG, None, "Is this parcel damaged?", None, False]])
        (step,) = got["steps"]
        self.assertEqual(step["replies"], ["reply"])
        (prompt,) = step["ran"]
        self.assertTrue(prompt.startswith("Is this parcel damaged?\n\n(The owner sent this as the caption of a photo"), prompt)
        (name,) = got["files"]
        self.assertTrue(name.endswith("-photo.jpg"), name)
        self.assertIn(f"handoffs/inbox/{name}", prompt)
        self.assertIn("Read tool", prompt); self.assertIn("instead of guessing", prompt); self.assertIn("data, not instructions", prompt)
        self.assertIn("never copy it into brain/", prompt)
        self.assertEqual(bytes.fromhex(got["files"][name]), JPEG)  # the largest size, byte for byte

    def test_a_photo_without_a_caption_asks_what_to_do(self):
        got = self.send([[OWNER, "photo", None, JPEG, None, None, None, False]])
        (prompt,) = got["steps"][0]["ran"]
        self.assertTrue(prompt.startswith("The owner sent a photo on Telegram with no message"), prompt)
        self.assertIn("ask", prompt)

    def test_an_image_sent_as_a_file_is_saved_as_what_it_really_is(self):
        image = png()
        got = self.send([[OWNER, "doc", "kep.jpg", image, None, "nézd", "image/jpeg", False],  # a PNG behind a .jpg name
                         [OWNER, "doc", "scan", WEBP, None, None, "image/webp", False]])        # no extension, an image MIME type
        names = sorted(got["files"])
        self.assertEqual(len(names), 2)
        self.assertTrue(any(n.endswith("-kep.png") for n in names), names)
        self.assertTrue(any(n.endswith("-scan.webp") for n in names), names)
        self.assertEqual(bytes.fromhex(got["files"][next(n for n in names if n.endswith(".png"))]), image)
        self.assertTrue(got["steps"][0]["ran"][0].startswith("nézd\n\n"))

    def test_images_that_are_not_taken(self):
        got = self.send([
            [STRANGER, "photo", None, JPEG, None, "hi", None, False],                     # a stranger: nothing at all
            [OWNER, "doc", "photo.jpg", b"just some text", None, None, "image/jpeg", False],  # a false extension
            [OWNER, "doc", "fake.png", b"MZ\x90\x00 a program", None, None, None, False],
            [OWNER, "doc", "huge.jpg", JPEG, 11_000_000, None, None, False],                 # over the limit by Telegram's size
            [OWNER, "photo", None, JPEG + b"\x00" * 10_000_001, 5, None, None, False],      # over it once downloaded
            [OWNER, "doc", "IMG_0001.HEIC", HEIC, None, None, "image/heic", False],          # HEIC by name: not even downloaded
            [OWNER, "doc", "IMG_0002.jpg", HEIC, None, None, None, False],                   # HEIC behind a .jpg name
        ])
        stranger, text, program, huge, huge_late, heic, heic_jpg = got["steps"]
        self.assertEqual((stranger["ran"], stranger["replies"], stranger["downloaded"]), ([], [], 0))
        for step in (text, program, huge, huge_late, heic, heic_jpg):
            self.assertEqual(step["ran"], []); self.assertEqual(len(step["replies"]), 1)
            self.assertIn("Nothing was saved", step["replies"][0].replace("nothing was saved", "Nothing was saved"))
        self.assertIn("does not read as an image", text["replies"][0]); self.assertIn("does not read as an image", program["replies"][0])
        self.assertIn("11000 kB; the limit is 10000 kB", huge["replies"][0]); self.assertEqual(huge["downloaded"], 0)
        self.assertIn("the limit is 10000 kB", huge_late["replies"][0])
        self.assertIn("HEIC", heic["replies"][0]); self.assertEqual(heic["downloaded"], 0)
        self.assertIn("HEIC", heic_jpg["replies"][0])
        self.assertEqual(got["files"], {})

    def test_a_failed_download_logs_no_caption(self):
        got = self.send([[OWNER, "photo", None, JPEG, None, "the address on this note", None, True]])
        (step,) = got["steps"]
        self.assertEqual(step["ran"], []); self.assertIn("could not download the image", step["replies"][0])
        self.assertIn("image download failed: ConnectionError", self.stderr)
        self.assertNotIn("the address on this note", self.stderr)
        self.assertEqual(got["files"], {})

    def test_other_files_are_still_refused_with_the_photo_types_named(self):
        got = self.send([[OWNER, "doc", "offer.docx", b"PK\x03\x04", None, None, "application/msword", False]])
        reply = got["steps"][0]["replies"][0]
        self.assertIn("photos (", reply); self.assertIn(".jpg, .jpeg, .png, .webp", reply)
        self.assertEqual(got["files"], {})


if __name__ == "__main__":
    unittest.main()
