"""Fail closed: a settle that reaches its cap, a booted count that does not match, a frame counter that answers nothing, a
foreign simulator, and result files that are never found by pattern. Real `run` processes against the stub guest."""
import concurrent.futures
import os
import re
import unittest

from macbench import core
from tests.test_dry import FAST, run_case, sections, sims_booted

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEP = ["scale", "--n", "1,2,3"] + FAST
_R = {}


def results():
    if not _R:
        cases = {"clean": (SWEEP, ""), "unsettled": (SWEEP, "unsettled@2"), "lost": (SWEEP, "lostsim@2"), "counter": (SWEEP, "counterfail@2"),
                 "foreign": (SWEEP, "foreign@2"),
                 "ab_lost": (["ab", "--knob", "live:group_x=1", "--n", "2", "--arm-settle", "0.2", "--secs", "3", "--settle-max", "2"], "lostsim@2"),
                 "soak_counter": (["soak", "--soak-n", "2", "--soak-minutes", "0.2", "--soak-every", "5", "--secs", "3", "--settle-max", "2"], "counterfail@2")}
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            futs = {k: ex.submit(run_case, a, f) for k, (a, f) in cases.items()}
            _R.update({k: f.result() for k, f in futs.items()})
    return _R


class SettleCap(unittest.TestCase):
    def test_a_settle_that_reaches_its_cap_fails_the_run_and_names_the_busy_processes(self):
        r = results()["unsettled"]
        self.assertNotEqual(r["rc"], 0, r["out"][-600:])
        s = sections(r["doc"], "settle")[0]
        self.assertEqual(s["status"], "FAIL")
        self.assertIn("NOT SETTLED at N=2", s["line"])
        self.assertIn("diagnosticd", s["line"])                              # the top processes by CPU at that moment
        self.assertIn("diagnosticd", [t["cmd"] for t in s["detail"]["top"]])
        self.assertEqual([st["n"] for st in r["doc"]["data"]["steps"]], [1])  # nothing was measured on top of it
        self.assertEqual(sections(r["doc"], "scale")[0]["status"], "FAIL")
        self.assertEqual(sims_booted(r["state"]), [])

    def test_a_clean_sweep_settles_every_step_and_says_how_long(self):
        r = results()["clean"]
        self.assertEqual(r["rc"], 0, r["out"][-600:])
        for st in r["doc"]["data"]["steps"]:
            self.assertTrue(st["settle"]["settled"])
            self.assertIsNotNone(st["settle"]["waited_s"])


class BootedCount(unittest.TestCase):
    def test_the_count_is_the_guests_not_the_expected_number(self):
        for st in results()["clean"]["doc"]["data"]["steps"]:
            self.assertEqual(st["booted"], st["n"])
            self.assertEqual(st["expected_booted"], st["n"])

    def test_a_mismatch_before_the_window_fails_the_run(self):
        r = results()["lost"]
        self.assertNotEqual(r["rc"], 0, r["out"][-600:])
        s = sections(r["doc"], "booted_check")[0]
        self.assertEqual(s["status"], "FAIL")
        self.assertIn("1 of the bench's simulators are booted, expected 2", s["line"])
        self.assertEqual(s["detail"], {"booted": 1, "expected": 2})            # the measured number is recorded
        self.assertEqual([st["n"] for st in r["doc"]["data"]["steps"]], [1])
        self.assertEqual(sims_booted(r["state"]), [])

    def test_ab_arms_and_every_other_window_are_counted_too(self):
        r = results()["ab_lost"]
        self.assertNotEqual(r["rc"], 0, r["out"][-600:])
        self.assertEqual(sections(r["doc"], "booted_check")[0]["status"], "FAIL")
        self.assertEqual(r["doc"]["data"].get("arms", []), [])
        live = os.path.join(r["state"], "live.conf")
        with open(live) as f:
            self.assertEqual(f.read(), "surface_coherent=1\n")

    def test_a_simulator_outside_the_bench_makes_the_run_yield(self):
        r = results()["foreign"]
        self.assertNotEqual(r["rc"], 0, r["out"][-600:])
        said = " | ".join(x["line"] for x in r["doc"]["sections"] if x["status"] == "FAIL")          # the precheck, the guard or the monitor: whoever sees it first
        self.assertRegex(said, r"YIELDING|someone needs the Mac|outside the bench are booted")
        self.assertEqual([st["n"] for st in r["doc"]["data"]["steps"]], [1])
        self.assertEqual(sims_booted(r["state"]), [])


class FrameCounter(unittest.TestCase):
    def test_a_counter_that_answers_nothing_fails_the_window(self):
        r = results()["counter"]
        self.assertNotEqual(r["rc"], 0, r["out"][-600:])
        st = r["doc"]["data"]["steps"][-1]
        self.assertEqual(st["n"], 2)
        self.assertEqual(st["gates"]["counter"]["status"], "FAIL")
        self.assertRegex(st["gates"]["counter"]["detail"], r"sim [01]: simfps: no surface")
        self.assertEqual(r["doc"]["gates"]["counter"]["status"], "FAIL")
        self.assertIn("gate failed at N=2: counter", r["doc"]["data"]["summary"]["stopped"])
        self.assertEqual(sims_booted(r["state"]), [])

    def test_a_soak_window_is_judged_too(self):
        r = results()["soak_counter"]
        self.assertEqual(r["doc"]["gates"]["counter"]["status"], "FAIL")
        self.assertEqual(sections(r["doc"], "gate:counter")[0]["status"], "FAIL")
        self.assertNotEqual(r["rc"], 0, r["out"][-600:])

    def test_gate_unit(self):
        ok = {"role": "busy", "simfps": {"fps": 60}, "raw": "1.0 5 255\n1.1 6 255\n"}
        self.assertEqual(core.counter_gate([ok, dict(ok, role="static", raw="1.0 -1 255\n")], "all-busy")["status"], "PASS")
        for bad in (dict(ok, simfps=None), dict(ok, simfps_error="hung"), dict(ok, raw="1.0 -1 255\n1.1 -1 255\n"), dict(ok, raw="")):
            self.assertEqual(core.counter_gate([ok, bad], "all-busy")["status"], "FAIL", bad)
        self.assertEqual(core.counter_gate([], "idle")["status"], "SKIP")


class ResultFilesAreNeverFoundByPattern(unittest.TestCase):
    """The base2 sweep read `f-busyK-*` and also got `f-busyK-unread-*`. Here every per-window file is opened by the exact
    path its owner built (a per-call temp directory; the window's own dict), nothing looks files up by prefix or glob."""

    def sources(self):
        for d in ("../macbench", "../guest"):
            for f in sorted(os.listdir(os.path.join(HERE, d))):
                if f.endswith(".py"):
                    with open(os.path.join(HERE, d, f)) as fh:
                        yield f, fh.read()

    def test_no_module_selects_files_by_pattern(self):
        for name, text in self.sources():
            self.assertNotRegex(text, r"\bimport glob\b|\bfrom glob\b|\bfnmatch\b|\.glob\(|\brglob\b", name)
        # the only directory listings: crash reports (exact suffix), Simlock's source set (twice: a UUID pattern, device.plist read by exact
        # path), the simslim config folder (one profile), /proc and /sys in hostprobe. A new listing has to be argued for here.
        allowed = {"benchguest.py": 4, "hostprobe.py": 2}
        for name, text in self.sources():
            n = len(re.findall(r"os\.listdir\(|os\.scandir\(", text))
            self.assertLessEqual(n, allowed.get(name, 0), name)

    def test_two_windows_of_one_step_keep_their_own_files(self):
        with open(os.path.join(HERE, "..", "guest", "benchguest.py")) as fh:
            src = fh.read()
        self.assertEqual(len(re.findall(r'tempfile\.mkdtemp\(prefix="benchwin"\)', src)), 1)       # one fresh directory per window
        self.assertIn('raws[u] = os.path.join(tmp, "raw-%d.txt" % i)', src)                         # exact name per simulator, kept in a dict


if __name__ == "__main__":
    unittest.main()
