"""DRY runs that follow each other in one stub guest: what a run leaves, what the next one refuses, the idle guard.
Each case is a real `run` process (stub guest, fake host); nothing real is touched."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_dry import FAST, RUN, run_case, sections, simctl_calls

ONE = ["scale", "--n", "1,2"] + FAST


def calls(state):
    with open(os.path.join(state, "guest", "calls.log")) as f:
        return [json.loads(l) for l in f]


def keep_calls(state):
    return [c for c in calls(state) if c[0].endswith("sim-idle-guard")]


def files(state, sub):
    try:
        return sorted(os.listdir(os.path.join(state, "guest", sub)))
    except OSError:
        return []


class CleanupAndLeftovers(unittest.TestCase):
    def test_a_cleanup_that_cannot_delete_says_so_exits_nonzero_and_blocks_the_next_run(self):
        state = tempfile.mkdtemp(prefix="dry-state-")
        r1 = run_case(ONE, "cantdelete", state=state)
        self.assertEqual(r1["rc"], 20, r1["out"][-800:])
        c = sections(r1["doc"], "cleanup")[0]
        self.assertEqual(c["status"], "FAIL")
        self.assertIn("could not clean up", c["line"])
        self.assertNotIn("CLEANUP PASS", r1["out"])
        self.assertTrue(os.path.exists(os.path.join(state, "guest", "set", ".bench-run.lock")))
        n_before = len([x for x in calls(state) if "clone" in x or "create" in x])
        r2 = run_case(ONE, "", state=state)                                    # the next run refuses to start
        self.assertEqual(r2["rc"], 2, r2["out"][-800:])
        self.assertIn("run `macbox bench cleanup`", r2["doc"]["sections"][0]["line"])
        self.assertEqual(n_before, len([x for x in calls(state) if "clone" in x or "create" in x]))   # it made nothing
        r3 = run_case(["cleanup"], "", state=state)                            # the explicit command cleans, verified
        self.assertEqual(r3["rc"], 0, r3["out"])
        self.assertIn("CLEANUP PASS", r3["out"])
        r4 = run_case(ONE, "", state=state)
        self.assertEqual(r4["rc"], 0, r4["out"][-800:])

    def test_cleanup_command_fails_loudly_when_it_cannot_delete(self):
        state = tempfile.mkdtemp(prefix="dry-state-")
        run_case(ONE, "cantdelete", state=state)
        r = run_case(["cleanup"], "cantdelete", state=state)
        self.assertEqual(r["rc"], 20)
        self.assertIn("CLEANUP FAIL", r["out"])


class IdleGuard(unittest.TestCase):
    def test_every_bench_simulator_is_kept_for_the_run_and_the_keep_files_are_removed(self):
        state = tempfile.mkdtemp(prefix="dry-state-")
        r = run_case(ONE, "", state=state)
        self.assertEqual(r["rc"], 0, r["out"][-600:])
        made = {c[2] for c in keep_calls(state)}
        self.assertEqual(len(made), 3)                            # the seed (made in this run) and the two clones
        for c in keep_calls(state):
            self.assertEqual(c[1], "keep")
            self.assertEqual(c[3], str(95))                      # --deadline 5400 s = 90 min + 5
        self.assertEqual(files(state, "sim-keep"), [])           # cleanup removed ~/.sim-keep/<udid>
        self.assertEqual(r["doc"]["env"]["guest"]["idle_guard"], "kept")

    def test_minutes_are_capped_at_240(self):
        state = tempfile.mkdtemp(prefix="dry-state-")
        r = run_case(ONE + ["--deadline", "30000"], "", state=state)
        self.assertEqual(r["rc"], 0, r["out"][-600:])
        self.assertTrue(keep_calls(state))
        self.assertEqual({c[3] for c in keep_calls(state)}, {"240"})

    def test_missing_tool_is_recorded_and_the_run_goes_on(self):
        r = run_case(ONE, "noguard")
        self.assertEqual(r["rc"], 0, r["out"][-600:])
        self.assertEqual(r["doc"]["env"]["guest"]["idle_guard"], "absent")
        s = sections(r["doc"], "idle_guard")[0]
        self.assertEqual(s["status"], "INFO")
        self.assertIn("ABSENT", s["line"])

    def test_a_refusing_tool_stops_the_run(self):
        r = run_case(ONE, "keepfail")
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("sim-idle-guard keep", r["out"])


if __name__ == "__main__":
    unittest.main()


def devices_of(state):
    with open(os.path.join(state, "guest", "state.json")) as f:
        return json.load(f)["devs"]


def kill_mid_seed(state):
    """A run killed outright (no signal handler, no exit trap, the guest helper gone with it) while its seed is half made:
    what an ssh drop or a SIGKILL leaves behind."""
    d = tempfile.mkdtemp(prefix="dry-kill-")
    env = dict(os.environ, DRY="1", BENCH_STUB_FAULT="slowslim", BENCH_DRY_KEEP=state, BENCH_DEADLINE_MARGIN="3")
    p = subprocess.Popen([sys.executable, RUN] + ONE + ["--out", os.path.join(d, "out")], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.STDOUT, start_new_session=True)
    t0 = time.time()
    try:
        while time.time() - t0 < 60 and not any(c[0].endswith("simslim") for c in (calls(state) if os.path.exists(os.path.join(state, "guest", "calls.log")) else [])):
            time.sleep(0.2)
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
    return any(c[0].endswith("simslim") for c in calls(state))


class HalfMadeSeed(unittest.TestCase):
    def test_a_killed_seed_is_refused_then_cleanup_removes_it_and_the_next_run_works(self):
        state = tempfile.mkdtemp(prefix="dry-half-")
        self.assertTrue(kill_mid_seed(state), "the run never reached simslim")
        left = devices_of(state)
        self.assertEqual(len(left), 1)                                           # the half-made seed
        self.assertEqual(files(state, "sim-keep"), list(left))
        r = run_case(ONE, "", state=state)                                       # the next run refuses to start on it
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("never completed", r["out"])
        self.assertIn("macbox bench cleanup", r["out"])
        c = run_case(["cleanup"], "", state=state)                               # a plain cleanup (no --seeds) removes it
        self.assertEqual(c["rc"], 0, c["out"][-600:])
        self.assertIn("CLEANUP PASS", c["out"])
        self.assertEqual(devices_of(state), {})
        self.assertEqual(files(state, "sim-keep"), [])
        self.assertFalse(os.path.exists(os.path.join(state, "guest", "set", ".bench-run.lock")))
        ok = run_case(ONE, "", state=state)
        self.assertEqual(ok["rc"], 0, ok["out"][-600:])
