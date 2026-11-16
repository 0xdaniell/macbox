"""The run's safety, without the rest: the watchdog, the one trap, knobs."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest


def rd(path):
    with open(path) as f:
        return f.read()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from macbench.runctx import LiveKnob, Run  # noqa: E402

SCRIPT = r'''
import sys, time
sys.path.insert(0, %r)
from macbench.runctx import Run
r = Run(6, print, margin=2)
r.teardown.append(("t", lambda: print("TEARDOWN RAN", flush=True)))
r.undo.append(("u", lambda: print("UNDO RAN", flush=True)))
r.on_deadline = lambda: print("PARTIAL WRITTEN", flush=True)
r.install()
mode = sys.argv[1]
if mode == "hang":
    time.sleep(60)
elif mode == "child":
    p = r.popen(["sleep", "60"])
    print("CHILD", p.pid, flush=True)
    time.sleep(60)
'''


class RunSafety(unittest.TestCase):
    def go(self, mode, sig=None, after=1.0):
        p = subprocess.Popen([sys.executable, "-c", SCRIPT % os.path.join(HERE, ".."), mode], stdout=subprocess.PIPE, text=True)
        if sig:
            time.sleep(after)
            p.send_signal(sig)
        out, _ = p.communicate(timeout=30)
        return p.returncode, out

    def test_deadline_runs_partial_write_then_cleanup_and_exits_124(self):
        t0 = time.time()
        rc, out = self.go("hang")
        self.assertEqual(rc, 124)
        self.assertLess(time.time() - t0, 10)
        self.assertLess(out.index("PARTIAL WRITTEN"), out.index("TEARDOWN RAN"))
        self.assertLess(out.index("TEARDOWN RAN"), out.index("UNDO RAN"))       # simulators down first, then knobs restored

    def test_sigterm_runs_cleanup_once_and_kills_children(self):
        rc, out = self.go("child", signal.SIGTERM)
        self.assertEqual(rc, 143)
        self.assertEqual(out.count("TEARDOWN RAN"), 1)
        pid = int(out.split("CHILD")[1].split()[0])
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_need_refuses_what_does_not_fit(self):
        from macbench.runctx import DeadlineError
        r = Run(100, margin=10)
        r.need(40, "x")
        with self.assertRaises(DeadlineError):
            r.need(80, "y")

    def test_live_knob_restores_bytes(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "live.conf")
        with open(p, "w") as f:
            f.write("a=1\n# comment\nb=2\nk=old\n")
        r = Run(100)
        k = LiveKnob(p, "k", "new", r)
        k.set()
        self.assertEqual(rd(p).splitlines()[-1], "k=new")
        self.assertNotIn("k=old", rd(p))
        k.unset()
        self.assertEqual(rd(p), "a=1\n# comment\nb=2\nk=old\n")
        k.set()
        r.cleanup()
        self.assertEqual(rd(p), "a=1\n# comment\nb=2\nk=old\n")


if __name__ == "__main__":
    unittest.main()


class GuardForeign(unittest.TestCase):
    def test_a_simulator_outside_the_bench_refuses_the_run(self):
        from macbench import core, fakes

        class A:
            min_host_free_gib, min_disk_gib, min_guest_free_gib, allow_foreign, disk_path = 1.0, 1.0, 1.0, False, "/"

        class G:
            def __init__(self, foreign):
                self.foreign = foreign

            def call(self, args, timeout):
                return {"mem": {"free_mb": 8000, "avail_mb": 9000}, "pressure": 1, "booted": 0, "foreign_booted": self.foreign}

        class Ctx:
            a, est_sim_mb, host = A(), 1000.0, fakes.FakeHost()
        c = Ctx()
        c.guest = G(1)
        self.assertIn("outside the bench", core.guard(c, 1, "x"))
        c.a.allow_foreign = True
        self.assertIsNone(core.guard(c, 1, "x"))
        c.guest = G(0)
        c.a.allow_foreign = False
        self.assertIsNone(core.guard(c, 1, "x"))
