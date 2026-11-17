"""DRY runs that follow each other in one stub guest: what a run leaves, what the next one refuses, the idle guard.
Each case is a real `run` process (stub guest, fake host); nothing real is touched."""
import json
import os
import tempfile
import unittest

from tests.test_dry import FAST, run_case, sections, simctl_calls

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
