"""Step 5: the owner's texts in the owner's language (core/texts.py, core/locale/<lang>.json).
Every language has every key with the same {fields}; what is missing or broken falls back to English; with JINNEE_LANG=hu
the bridge, the alerts, the schedule and the dashboard speak Hungarian, while the agents' prompt and the commands do not change."""
import json, os, pathlib, sys, tempfile, time, unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
os.environ["JINNEE_LANG"] = "en"  # the default for this module; the Hungarian tests switch it per test
sys.path.insert(0, str(REPO / "core"))
import texts, alerts, approvals, autonomy, memory_guard, scheduler, workfile
from test_alerts import Bridge, Watching, OWNER, at

LOCALE = REPO / "core" / "locale"


def hu():
    return mock.patch.dict(os.environ, {"JINNEE_LANG": "hu"})


class Dictionaries(unittest.TestCase):
    def setUp(self):
        self.en = json.loads((LOCALE / "en.json").read_text(encoding="utf-8"))
        self.hu = json.loads((LOCALE / "hu.json").read_text(encoding="utf-8"))

    def test_every_key_is_in_both_languages(self):
        en = {k for k in self.en if not k.startswith("_")}
        hu = {k for k in self.hu if not k.startswith("_")}
        self.assertEqual(sorted(en - hu), [], "missing from hu.json (shown in English until translated)")
        self.assertEqual(sorted(hu - en), [], "in hu.json but not in en.json: a typo, or en.json is missing it")

    def test_the_fields_are_the_same_in_both(self):
        for k, v in self.en.items():
            if k.startswith("_"): continue
            self.assertEqual(texts.fields(self.hu[k]), texts.fields(v), k)

    def test_the_check_command_agrees(self):
        self.assertEqual(texts.problems(), [])

    def test_every_language_file_is_checked_against_english(self):
        for p in LOCALE.glob("*.json"):
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertTrue(all(isinstance(v, str) for v in data.values()), p.name)

    def test_hungarian_reads_as_written_for_people(self):
        for k, v in self.hu.items():
            self.assertNotIn("—", v, k)  # no em-dash
            self.assertNotRegex(v, r"(?i)kedves felhasználó|tájékoztatjuk|örömmel", k)
            self.assertNotRegex(v, r"\b(Ön|Önnek|Önt|kérjük)\b", k)  # informal: te, not Ön

    def test_the_commands_and_marks_are_not_translated(self):
        for k, v in self.en.items():
            for cmd in ("/ok", "/change", "/drop", "/undo", "/pending", "/continue", "/schedule", "/new", "/status", "/login"):
                if cmd in v: self.assertIn(cmd, self.hu[k], (k, cmd))
            for mark in ("brain/lessons.md", "config/business.yaml", "JINNEE_MEMORY_LESSONS_BYTES", "DASHBOARD_PASSWORD"):
                if mark in v: self.assertIn(mark, self.hu[k], (k, mark))


class Fallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / "en.json").write_text(json.dumps({"a": "Hello {name}", "b": "Bye", "_note": "not shown"}), encoding="utf-8")
        p = mock.patch.object(texts, "LOCALE", self.dir); p.start(); self.addCleanup(p.stop)
        texts._cache.clear(); self.addCleanup(texts._cache.clear)

    def write(self, lang, data):
        (self.dir / f"{lang}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        texts._cache.pop(lang, None)

    def test_a_missing_key_comes_out_in_english(self):
        self.write("hu", {"a": "Szia {name}"})
        self.assertEqual(texts.t("a", "hu", name="X"), "Szia X")
        self.assertEqual(texts.t("b", "hu"), "Bye")
        self.assertEqual(texts.problems(), ["locale/hu.json: 'b' is missing (English is shown)"])

    def test_an_unknown_or_broken_language_is_english(self):
        self.assertEqual(texts.t("a", "xx", name="X"), "Hello X")
        (self.dir / "de.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(texts.t("b", "de"), "Bye")
        self.assertIn("locale/de.json is empty or cannot be read", texts.problems())

    def test_a_translation_with_the_wrong_fields_falls_back(self):
        self.write("hu", {"a": "Szia {nev}", "b": "Szia"})
        with self.assertLogs("jinnee.texts", "WARNING"):
            self.assertEqual(texts.t("a", "hu", name="X"), "Hello X")
        self.assertIn("locale/hu.json: 'a' has the fields ['nev'], English has ['name']", texts.problems())

    def test_an_extra_key_is_reported(self):
        self.write("hu", {"a": "Szia {name}", "b": "Szia", "c": "?"})
        self.assertEqual(texts.problems(), ["locale/hu.json: 'c' is not a key in en.json"])

    def test_a_key_not_even_in_english_is_a_bug(self):
        with self.assertRaises(KeyError): texts.t("nope")

    def test_language_codes(self):
        for raw, want in (("hu", "hu"), ("HU", "hu"), ("hu-HU", "hu"), ("hu_HU", "hu"), (" en ", "en"), ("", "en"), ("12", "en")):
            self.assertEqual(texts.code(raw), want, raw)

    def test_the_language_comes_from_the_settings_and_is_read_each_time(self):
        self.write("hu", {"a": "Szia {name}", "b": "Szia"})
        with hu(): self.assertEqual(texts.t("b"), "Szia")
        with mock.patch.dict(os.environ, {"JINNEE_LANG": "hu-HU"}): self.assertEqual(texts.t("b"), "Szia")
        with mock.patch.dict(os.environ, {"JINNEE_LANG": ""}):
            with mock.patch.dict(os.environ, {"JINNEE_BUSINESS_FILE": "/nonexistent/business.yaml"}):
                self.assertEqual(texts.t("b"), "Bye")  # no setting at all: English
        self.assertEqual(texts.bundle("a"), {"a": "Hello {name}"})


class HungarianCore(Watching):
    """The core modules, in process, with JINNEE_LANG=hu."""

    def test_waiting_and_the_buttons_go_by_the_message_not_its_words(self):
        a = self.add(at(3, 9))
        with hu(): plan = self.watch.due(at(3, 10))
        self.assertTrue(plan["messages"][0].startswith("Döntésedre vár:"), plan["messages"][0])
        self.assertIn(f"/ok {a} · /change {a} mi változzon · /drop {a} miért", plan["messages"][0])
        self.assertEqual(plan["ask_text"], plan["messages"][0])  # the bridge puts the tap buttons under exactly this one
        self.assertEqual(plan["ask"], [a])

    def test_statuses_reminders_and_level_names(self):
        a = self.add(at(3, 9))
        with hu():
            self.assertEqual(self.book.status(self.book.get(a)), "rád vár")
            self.assertEqual(self.book.status(self.book.get(a), "en"), "waiting for the owner")
            self.assertEqual(alerts.ago(3 * 86400), "3 napja")
            self.assertEqual(autonomy.name(1), "jóváhagyás kell")
            self.assertEqual(autonomy.label("raise_autonomy", {}), "Önállósági szint módosítása")
            with self.assertRaises(approvals.Refused) as e: self.book.decide(a, "drop", "")
            self.assertIn("indok kell", str(e.exception))
        self.assertEqual(autonomy.NAMES[1], "approval required")  # the prompt and the command line keep English

    def test_the_prompt_and_the_hand_over_stay_english(self):
        a = self.add(at(3, 9))
        with mock.patch("time.time", return_value=at(3, 9)): self.book.decide(a, "ok")
        with hu():
            self.assertIn("approved, not picked up yet", self.book.brief())
            self.assertIn("approved, not picked up yet", alerts.hand_over_message(self.book, [self.book.get(a)]))
            self.assertIn("approval required", self.book.gate.describe())

    def test_the_schedule_in_hungarian(self):
        now = at(3, 12)
        with hu():
            self.assertEqual(scheduler.when_text(now + 3600, now), "ma 13:00")
            self.assertEqual(scheduler.when_text(now + 86400, now), "holnap 12:00")
            self.assertEqual(scheduler.when_text(now + 3 * 86400, now), "péntek 12:00")
            self.assertEqual(scheduler.when_text(None), "soha")
            self.assertEqual(scheduler.result_text("never ran"), "még sosem futott")
            self.assertEqual(scheduler.result_text("missed", "ma 08:00"), "kimaradt: ma 08:00")
            summary = scheduler.Scheduler(self.brain).summary(now)
        self.assertTrue(summary.startswith("Ütemezés ("), summary)
        self.assertIn("Jinnee: reggeli összefoglaló", summary)
        self.assertIn("config/business.yaml fájlban tudsz (schedule.jobs)", summary)
        self.assertIn("kell hozzá: competitors (még nem neveztél meg versenytársat", summary)
        self.assertEqual(scheduler.when_text(now + 3 * 86400, now), "Fri 12:00")  # English again outside hu()

    def test_work_files_and_memory(self):
        root = pathlib.Path(self.tmp.name)
        (root / "w.md").write_text("<!-- work: in-progress -->\n# x\n- [x] one\n- [ ] two\n", encoding="utf-8")
        with hu():
            self.assertEqual(workfile.summary(root, ["w.md"]), "• w.md: 2 lépésből 1 kész: one. Következő: two")
            lines = memory_guard.notice([], 9000)
        self.assertIn("[pin]", " ".join(lines))
        self.assertTrue(lines[0].startswith("A brain/lessons.md 9000 bájt"), lines[0])


class HungarianBridge(Bridge):
    """The Telegram bridge itself, started from a copy of the repo with JINNEE_LANG=hu in its .env."""

    def dotenv(self, text):
        super().dotenv(text + "JINNEE_LANG=hu\n")

    def test_the_owner_commands_answer_in_hungarian(self):
        a = self.add()
        got = self.drive([["msg", OWNER, "/pending"], ["msg", OWNER, f"/ok {a}"], ["msg", OWNER, f"/undo {a}"],
                          ["msg", OWNER, f"/drop {a}"], ["msg", OWNER, "/nope"], ["msg", OWNER, "/status"],
                          ["msg", OWNER, "/continue"]])
        self.assertIn(f"[{a}] Reply to Anna", got[0]["replies"][0])
        self.assertIn("rád vár", got[0]["replies"][0])
        self.assertIn("/ok <azonosító>", got[0]["replies"][0])
        self.assertTrue(got[1]["replies"][0].startswith("Jóváhagyva: "), got[1]["replies"][0])
        self.assertIn(f"/undo_{a}", got[1]["replies"][0])
        self.assertTrue(got[2]["replies"][0].startswith("Visszavonva."), got[2]["replies"][0])
        self.assertIn("Miért veted el?", got[3]["replies"][0])
        self.assertTrue(got[4]["replies"][0].startswith("Parancsok: /pending"), got[4]["replies"][0])
        self.assertIn("Most épp semmin nem dolgozom.", got[5]["replies"][0])
        self.assertIn("Döntésedre vár: 1.", got[5]["replies"][0])
        self.assertEqual(got[6]["replies"], ["Semmi nem szakadt félbe és nem maradt befejezetlen, úgyhogy nincs mit folytatni."])
        self.assertEqual(self.state(a)[0], "pending")

    def test_the_minute_check_says_it_in_hungarian(self):
        a = self.add()
        got = self.drive([["tick", 0, ""]])
        self.assertEqual(len(got[0]["sent"]), 1)
        self.assertTrue(got[0]["sent"][0][1].startswith("Döntésedre vár:"), got[0]["sent"][0][1])
        self.assertIn(f"/drop {a} miért", got[0]["sent"][0][1])

    def test_a_failed_run_is_explained_in_hungarian(self):
        got = self.drive([["msg", OWNER, "hello"]], agent="real", extra=self.fake_claude("echo 'boom' >&2; exit 1\n"))
        reply = got[0]["replies"][0]
        self.assertTrue(reply.startswith("Ezt nem tudtam befejezni"), reply)
        self.assertIn("Hiba: boom", reply)


class HungarianDashboard(unittest.TestCase):
    """The page is served with the owner's words in it; the state's labels, levels and schedule results too."""

    def setUp(self):
        import test_dashboard
        self.base = test_dashboard.Base("run")
        self.base.setUp(); self.addCleanup(self.base.doCleanups)

    def test_page_login_and_state_in_hungarian(self):
        with hu():
            c = self.base.client()
            page = c.get("/").text
            state = c.get("/api/state").json()
            refused = c.post("/api/approve/a1", json={"decision": "drop", "note": ""}, headers={"origin": "http://localhost:8080"})
        self.assertIn('<html lang="hu">', page)
        self.assertIn("<h2>Döntésedre vár</h2>", page)
        self.assertNotRegex(page, r"\{\{(t:|TEXTS|NAME)")  # every slot filled
        words = json.loads(page.split("const T=", 1)[1].split(";\n", 1)[0])
        self.assertEqual(words["dash.approve"], "Jóváhagyom")
        self.assertEqual(state["approvals"][0]["status"], "rád vár")
        levels = {a["action"]: a for a in state["autonomy"]}
        self.assertEqual((levels["raise_autonomy"]["label"], levels["raise_autonomy"]["name"]), ("Önállósági szint módosítása", "jóváhagyás kell"))
        self.assertEqual(refused.status_code, 400); self.assertIn("indok kell", refused.json()["detail"])

    def test_english_by_default(self):
        page = self.base.client().get("/").text
        self.assertIn('<html lang="en">', page); self.assertIn("<h2>Awaiting your decision</h2>", page)


if __name__ == "__main__":
    unittest.main()
