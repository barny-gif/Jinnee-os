"""install.sh: where the answers come from. Run: python -m unittest discover tests
The real script runs, piped into bash the way the README one-liner does it, with a pseudo-terminal as the
controlling terminal. git, docker, node, npm, claude, pip3, nohup, curl, sudo and brew are stand-ins on PATH
that only write down how they were called, so nothing is installed, downloaded or started."""
import os, pathlib, select, shutil, subprocess, sys, tempfile, time, unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
import env

FAKE = """#!/bin/sh
name=$(basename "$0")
echo "$name $*" >> "$FAKE_LOG"
case "$name $1" in
  "git clone") cp -R "$FAKE_SRC" "$4" ;;
  "claude login"|"docker compose")
    case "$*" in *login)
      if [ -t 0 ]; then printf 'Paste code: '; read -r code; echo "login got: $code" >> "$FAKE_LOG"
      else echo "login without a terminal" >> "$FAKE_LOG"; fi ;;
    esac ;;
  "nohup python3") echo "nohup env: owner=${TELEGRAM_OWNER_ID-unset} token=${TELEGRAM_BOT_TOKEN-unset} name=${JINNEE_NAME-unset}" >> "$FAKE_LOG" ;;
esac
"""
FAKED = ("git", "docker", "node", "npm", "claude", "pip3", "nohup", "curl", "sudo", "brew")
OURS = ("JINNEE_MODE", "JINNEE_NAME", "JINNEE_LANG", "JINNEE_DIR", "PACKS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID")
PIPED = "cat install.sh | bash"  # same shape as `curl … | bash`: the script is on stdin
TOKEN = "123456:TEST-not_a-real-token"


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs bash and a pseudo-terminal")
class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = pathlib.Path(self.tmp.name)
        self.work, self.home, src, fakebin = tmp / "work", tmp / "home", tmp / "src", tmp / "bin"
        for d in (self.work, self.home, fakebin): d.mkdir()
        shutil.copy(REPO / "install.sh", self.work / "install.sh")
        for d in ("core", "packs"):
            shutil.copytree(REPO / d, src / d, ignore=shutil.ignore_patterns("__pycache__"))
        for f in (".env.example", "requirements.txt"): shutil.copy(REPO / f, src / f)
        for name in FAKED:
            (fakebin / name).write_text(FAKE); (fakebin / name).chmod(0o755)
        self.log, self.dir = tmp / "calls.log", self.home / "jinnee-os"
        real = dict.fromkeys(os.path.dirname(shutil.which(c)) for c in ("bash", "python3", "cat", "cp", "uname", "mkdir", "rm", "basename"))
        self.env = {k: v for k, v in os.environ.items() if k not in OURS}
        self.env.update(HOME=str(self.home), PATH=os.pathsep.join([str(fakebin), *real]), FAKE_LOG=str(self.log),
                        FAKE_SRC=str(src), PYTHONDONTWRITEBYTECODE="1")

    def calls(self, started=0):
        """What the stand-ins wrote down. started: how many backgrounded programs to wait for (they outlive the installer)."""
        for _ in range(100):
            lines = self.log.read_text().splitlines() if self.log.exists() else []
            if len([c for c in lines if c.startswith("nohup env")]) >= started: break
            time.sleep(0.1)
        return lines

    def dotenv(self):
        return env.parse((self.dir / ".env").read_text(encoding="utf-8"))

    def in_terminal(self, command, dialogue, extra=None):
        """Runs `command` with a pseudo-terminal as its controlling terminal. dialogue: (text to wait for, line to type
        or None to type nothing). Returns (exit code, everything shown on the terminal)."""
        import pty
        # The stand-in nohup does not survive the terminal closing the way the real one does, so the terminal
        # stays open until the backgrounded stand-ins have written their line.
        linger = command + '; code=$?; n=0; while [ $n -lt 50 ] && grep -q "^nohup python3" "$FAKE_LOG" 2>/dev/null' \
                           ' && [ "$(grep -c "^nohup env" "$FAKE_LOG")" -lt 2 ]; do sleep 0.1; n=$((n+1)); done; exit $code'
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.work)
            os.execve(shutil.which("bash"), ["bash", "-c", linger], {**self.env, **(extra or {})})
        shown, seen, deadline = "", 0, time.time() + 60

        def read_until(text):
            nonlocal shown, seen
            while text is None or text not in shown[seen:]:
                if time.time() > deadline or not select.select([fd], [], [], 1)[0]:
                    if time.time() > deadline:
                        os.kill(pid, 9); os.waitpid(pid, 0)
                        self.fail(f"timed out waiting for {text!r}. Terminal so far:\n{shown}")
                    continue
                try: chunk = os.read(fd, 4096)
                except OSError: chunk = b""  # Linux reports the closed terminal as EIO
                if not chunk:
                    if text is None: return
                    os.waitpid(pid, 0)
                    self.fail(f"ended while waiting for {text!r}. Terminal so far:\n{shown}")
                shown += chunk.decode("utf-8", "replace")
            seen = shown.index(text, seen) + len(text)

        try:
            for wait_for, line in dialogue:
                read_until(wait_for)
                if line is not None: os.write(fd, (line + "\n").encode())
            read_until(None)
        finally:
            os.close(fd)
        return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), shown

    def no_terminal(self, extra=None):
        """Piped script, stdin empty, own session: there is no controlling terminal, so /dev/tty cannot be opened."""
        return subprocess.run(["bash", "-c", PIPED], cwd=self.work, env={**self.env, **(extra or {})}, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60, start_new_session=True)

    def test_piped_script_asks_on_the_terminal(self):
        name = "Mia & Co | #1"
        code, shown = self.in_terminal(PIPED, [
            ("Where should it run?", "3"), ("Answer 1 (Docker) or 2", None), ("Where should it run?", "2"),
            ("Telegram bot token", ""), ("The token is required", None), ("Telegram bot token", TOKEN),
            ("Your Telegram user ID", ""), ("A number is required", None), ("Your Telegram user ID", "@someone"),
            ("A number is required", None), ("Your Telegram user ID", "0"), ("A number is required", None),
            ("Your Telegram user ID", " 4242 "),
            ("be called?", name), ("Pack:", "x"), ("Answer 1 or 2", None), ("Pack:", "2"),
            ("Paste code:", "the-login-code"),
        ])
        self.assertEqual(code, 0, shown)
        self.assertIn(f"Done. Message {name} on Telegram", shown)
        got = self.dotenv()
        self.assertEqual((got["TELEGRAM_BOT_TOKEN"], got["TELEGRAM_OWNER_ID"], got["JINNEE_NAME"], got["PACKS"]),
                         (TOKEN, "4242", name, "general,ecom"))
        example = (REPO / ".env.example").read_text().splitlines()
        written = (self.dir / ".env").read_text().splitlines()
        self.assertEqual(len(written), len(example))  # only the four lines changed
        self.assertEqual([w for w, e in zip(written, example) if w != e],
                         [f"JINNEE_NAME='{name}'", f"TELEGRAM_BOT_TOKEN={TOKEN}", "TELEGRAM_OWNER_ID=4242", "PACKS=general,ecom"])
        self.assertFalse((self.dir / ".env.new").exists())
        calls = self.calls(started=2)
        self.assertIn("claude login", calls)
        self.assertIn("login got: the-login-code", calls)  # the interactive step got the terminal, not the pipe
        self.assertEqual(sorted(c for c in calls if c.startswith("nohup python3")),
                         ["nohup python3 core/jinnee.py", "nohup python3 dashboard/app.py"])
        self.assertFalse([c for c in calls if c.split()[0] in ("curl", "sudo", "docker", "brew", "npm")], calls)
        self.assertTrue((self.dir / "brain" / "approvals.json").exists())  # pack_loader --init ran for real

    def test_run_as_a_file_in_docker_mode_with_defaults(self):
        code, shown = self.in_terminal("bash install.sh", [
            ("Where should it run?", "1"), ("Telegram bot token", TOKEN), ("Your Telegram user ID", "4242"),
            ("be called?", ""), ("Pack:", ""), ("Paste code:", "abc"),
        ])
        self.assertEqual(code, 0, shown)
        got = self.dotenv()
        self.assertEqual((got["JINNEE_NAME"], got["PACKS"], got["TELEGRAM_OWNER_ID"]), ("Jinnee", "general", "4242"))
        calls = self.calls()
        self.assertIn("docker compose up -d --build", calls)
        self.assertIn("docker compose exec jinnee claude login", calls)
        self.assertIn("login got: abc", calls)
        self.assertFalse([c for c in calls if c.startswith(("nohup", "pip3", "claude"))], calls)

    def test_variables_replace_questions_and_a_bad_one_is_asked_again(self):
        name = """J "$HOME" \\1 `id`"""
        code, shown = self.in_terminal(PIPED, [("Your Telegram user ID", "777"), ("be called?", name), ("Pack:", "1"), ("Paste code:", "c")],
                                       {"JINNEE_MODE": "native", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_OWNER_ID": "not-a-number"})
        self.assertEqual(code, 0, shown)
        self.assertNotIn("Where should it run?", shown)
        self.assertNotIn("Telegram bot token", shown)
        self.assertNotIn(TOKEN, shown)  # a token from the environment is not printed
        self.assertIn("TELEGRAM_OWNER_ID is set, but not usable", shown)
        got = self.dotenv()
        self.assertEqual((got["TELEGRAM_OWNER_ID"], got["JINNEE_NAME"], got["PACKS"]), ("777", name, "general"))
        # the rejected value does not reach the started programs through the environment; .env is the only source
        self.assertEqual([c for c in self.calls(started=2) if c.startswith("nohup env")],
                         ["nohup env: owner=unset token=unset name=unset"] * 2)

    def test_closed_terminal_stops_instead_of_looping(self):
        code, shown = self.in_terminal(PIPED, [("Where should it run?", "2"), ("Telegram bot token", "\x04")])  # Ctrl-D
        self.assertNotEqual(code, 0)
        self.assertIn("Nothing was installed", shown)
        self.assertEqual(self.calls(), [])

    def test_no_terminal_and_no_answers_stops_before_installing(self):
        r = self.no_terminal()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no terminal to ask", r.stderr)
        self.assertIn("not set: JINNEE_MODE TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_ID\n", r.stderr)
        self.assertIn("TELEGRAM_OWNER_ID=… bash", r.stderr)  # shows how to pass the answers
        self.assertEqual(self.calls(), [])  # not even git
        self.assertFalse(self.dir.exists())

    def test_no_terminal_with_variables_installs_without_questions(self):
        r = self.no_terminal({"JINNEE_MODE": "native", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_OWNER_ID": "4242",
                              "JINNEE_NAME": "Ops Bot", "PACKS": "general,ecom"})
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.dotenv()
        self.assertEqual((got["TELEGRAM_BOT_TOKEN"], got["TELEGRAM_OWNER_ID"], got["JINNEE_NAME"], got["PACKS"]),
                         (TOKEN, "4242", "Ops Bot", "general,ecom"))
        calls = self.calls(started=2)
        self.assertNotIn("claude login", calls)  # cannot be done without a terminal; the owner is told instead
        self.assertIn("One step is left, and it needs a terminal. Ops Bot cannot answer until it is done: claude login", r.stdout)
        self.assertEqual(len([c for c in calls if c.startswith("nohup python3")]), 2)

    def test_no_terminal_name_and_packs_are_optional(self):
        r = self.no_terminal({"JINNEE_MODE": "docker", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_OWNER_ID": "4242"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dotenv()["JINNEE_NAME"], self.dotenv()["PACKS"]), ("Jinnee", "general"))
        self.assertIn("docker compose up -d --build", self.calls())
        self.assertNotIn("docker compose exec jinnee claude login", self.calls())
        self.assertIn("docker compose exec jinnee claude login", r.stdout)

    def test_no_terminal_rejects_a_missing_or_bad_owner_and_token(self):
        good = {"JINNEE_MODE": "native", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_OWNER_ID": "4242"}
        for var, bad in (("TELEGRAM_OWNER_ID", ""), ("TELEGRAM_OWNER_ID", "0"), ("TELEGRAM_OWNER_ID", "@me"),
                         ("TELEGRAM_OWNER_ID", "12 34"), ("TELEGRAM_OWNER_ID", "-5"), ("TELEGRAM_BOT_TOKEN", ""),
                         ("TELEGRAM_BOT_TOKEN", "two words"), ("JINNEE_MODE", "vps"), ("PACKS", "general;rm -rf")):
            with self.subTest(var=var, bad=bad):
                r = self.no_terminal({**good, var: bad})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(var, r.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.dir.exists())


if __name__ == "__main__":
    unittest.main()
