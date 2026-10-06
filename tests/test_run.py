"""run.sh: one copy of each program, however often it is started. Run: python -m unittest discover tests
The real script runs in a temp folder. `python3` on PATH is a stand-in that just stays alive under the same command line,
so real processes are started, found and stopped, but neither the bridge nor the dashboard actually runs."""
import os, pathlib, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
SLEEPER = f'#!/bin/sh\necho "started $*"\nexec "{sys.executable}" -c "import time; time.sleep(300)" "$@"\n'


@unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("ps"), "needs bash and ps")
class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name).resolve() / "jinnee-os"
        fakebin = pathlib.Path(self.tmp.name) / "bin"
        for d in (self.dir / "core", self.dir / "dashboard", fakebin): d.mkdir(parents=True)
        shutil.copy(REPO / "run.sh", self.dir / "run.sh")
        (fakebin / "python3").write_text(SLEEPER); (fakebin / "python3").chmod(0o755)
        self.env = {**os.environ, "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
        self.env.pop("JINNEE_LOG_DIR", None)
        self.addCleanup(self.run_sh, "stop")

    def run_sh(self, *args):
        return subprocess.run(["bash", "run.sh", *args], cwd=self.dir, env=self.env, capture_output=True, text=True, timeout=60)

    def pids(self):
        return {n: int((self.dir / "logs" / f"{n}.pid").read_text()) for n in ("jinnee", "dashboard") if (self.dir / "logs" / f"{n}.pid").exists()}

    def alive(self, pid):
        try: os.kill(pid, 0)
        except ProcessLookupError: return False
        with open(f"/proc/{pid}/stat") as f: return f.read().rsplit(")", 1)[1].split()[0] != "Z" if os.path.exists(f"/proc/{pid}/stat") else True

    def copies(self):
        """Processes running one of the two programs from this folder, whoever started them."""
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
        found = []
        for line in out.splitlines():
            pid, _, args = line.strip().partition(" ")
            if ("core/jinnee.py" in args or "dashboard/app.py" in args) and "time.sleep" in args:
                try: here = os.readlink(f"/proc/{pid}/cwd") == str(self.dir)
                except OSError: here = False
                if here: found.append(int(pid))
        return sorted(found)

    def test_start_twice_is_still_one_copy_each(self):
        r = self.run_sh("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        first = self.pids()
        self.assertEqual(sorted(first), ["dashboard", "jinnee"])
        self.assertEqual(self.copies(), sorted(first.values()))
        r = self.run_sh("start")
        self.assertIn("jinnee is already running", r.stdout); self.assertIn("dashboard is already running", r.stdout)
        self.assertEqual(self.pids(), first)
        self.assertEqual(self.copies(), sorted(first.values()))
        self.assertEqual(self.run_sh("status").returncode, 0)

    def test_restart_replaces_and_stop_stops(self):
        self.run_sh("start")
        first = self.pids()
        r = self.run_sh("restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        second = self.pids()
        self.assertFalse(set(first.values()) & set(second.values()))
        self.assertFalse(any(self.alive(p) for p in first.values()))
        self.assertEqual(self.copies(), sorted(second.values()))
        self.run_sh("stop")
        self.assertEqual((self.copies(), self.pids()), ([], {}))
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1); self.assertIn("jinnee is not running", r.stdout)
        self.assertIn("was not running", self.run_sh("stop").stdout)  # stopping twice is fine

    def test_a_stale_pid_file_is_not_trusted(self):
        """After a reboot the number in the file can belong to anything. It is neither taken for ours nor stopped."""
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(lambda: (stranger.kill(), stranger.wait()))
        (self.dir / "logs").mkdir()
        (self.dir / "logs" / "jinnee.pid").write_text(f"{stranger.pid}\n")
        (self.dir / "logs" / "dashboard.pid").write_text("not a number\n")
        r = self.run_sh("start")
        self.assertIn("jinnee started", r.stdout); self.assertIn("dashboard started", r.stdout)
        self.assertNotEqual(self.pids()["jinnee"], stranger.pid)
        self.assertEqual(len(self.copies()), 2)
        (self.dir / "logs" / "jinnee.pid").write_text(f"{stranger.pid}\n")
        self.run_sh("stop")
        self.assertIsNone(stranger.poll())  # still alive: stop did not touch it
        self.assertEqual(self.copies(), [])  # and the real copy was found and stopped anyway

    @unittest.skipUnless(os.path.exists("/proc/self/cwd") and shutil.which("pgrep"), "needs /proc and pgrep")
    def test_a_copy_started_by_the_old_installer_is_replaced(self):
        old = subprocess.Popen("nohup python3 core/jinnee.py >> logs.txt 2>&1 &", shell=True, cwd=self.dir, env=self.env)
        old.wait()
        elsewhere = pathlib.Path(self.tmp.name) / "other"; (elsewhere / "core").mkdir(parents=True)
        other = subprocess.Popen(["python3", "core/jinnee.py"], cwd=elsewhere, env=self.env, stdout=subprocess.DEVNULL)  # another install
        self.addCleanup(lambda: (other.kill(), other.wait()))
        for _ in range(50):
            if self.copies(): break
            time.sleep(0.1)
        stray = self.copies()
        self.assertEqual(len(stray), 1)
        r = self.run_sh("start")
        self.assertIn("stopping a copy started earlier", r.stdout)
        self.assertEqual(self.copies(), sorted(self.pids().values()))
        self.assertNotIn(stray[0], self.copies())
        self.assertIsNone(other.poll())  # the other folder's copy is none of our business

    def test_output_goes_to_the_logs_folder(self):
        self.run_sh("start")
        for _ in range(50):
            if (self.dir / "logs" / "jinnee.out").read_text(): break
            time.sleep(0.1)
        self.assertEqual((self.dir / "logs" / "jinnee.out").read_text(), "started core/jinnee.py\n")
        self.assertFalse((self.dir / "logs.txt").exists())
        self.assertEqual(self.run_sh().returncode, 2)


class Rotation(unittest.TestCase):
    def test_log_file_rotates_and_hides_the_token(self):
        import logging
        sys.path.insert(0, str(REPO / "core"))
        import logs
        root = logging.getLogger()
        saved = (root.handlers[:], root.level)
        self.addCleanup(lambda: (root.handlers.__setitem__(slice(None), saved[0]), root.setLevel(saved[1])))
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"JINNEE_LOG_DIR": d}):
                self.assertTrue(logs.setup("jinnee"))
            for i in range(40000): logging.getLogger("jinnee").info("line %s %s", i, "x" * 100)
            logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot123456:SECRET/getUpdates")
            for h in root.handlers: h.close()
            files = sorted(p.name for p in pathlib.Path(d).iterdir())
            self.assertEqual(files, ["jinnee.log", "jinnee.log.1", "jinnee.log.2", "jinnee.log.3"])  # 4.6 MB written, 4 MB kept at most
            self.assertLessEqual(sum(p.stat().st_size for p in pathlib.Path(d).iterdir()), 4_100_000)
            self.assertFalse(any("SECRET" in p.read_text() for p in pathlib.Path(d).iterdir()))
        with mock.patch.dict(os.environ, {"JINNEE_LOG_DIR": ""}):
            self.assertFalse(logs.setup("jinnee"))


if __name__ == "__main__":
    unittest.main()
