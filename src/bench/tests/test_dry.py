"""DRY=1 runs of the whole suite against the stub guest and a fake host: control flow, faults, safety, outputs.
Each case is a real `run` process (its own signal handlers, its own exit trap). They run in parallel."""
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "..", "run")
FAST = ["--secs", "3", "--probe-every", "0.7", "--probe-timeout", "1", "--settle-max", "2", "--no-observer-check"]


def run_case(args, faults="", extra_env=None, kill_after=None, keep=True, timeout=240, state=None):
    """One real `run` process in DRY mode. `state` = a directory shared by several cases in a row (the stub guest, its
    device set, the seed): the run after another one sees what the first left."""
    d = tempfile.mkdtemp(prefix="dry-case-")
    env = dict(os.environ, DRY="1", BENCH_STUB_FAULT=faults, BENCH_DRY_KEEP=state or os.path.join(d, "state") if keep else "",
               BENCH_DEADLINE_MARGIN="3", **(extra_env or {}))
    if not keep:
        env.pop("BENCH_DRY_KEEP")
    out_arg = [] if args[0] == "cleanup" else ["--out", os.path.join(d, "out")]
    p = subprocess.Popen([sys.executable, RUN] + args + out_arg, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    if kill_after:
        time.sleep(kill_after)
        p.send_signal(signal.SIGTERM)
    out, _ = p.communicate(timeout=timeout)
    res = None
    od = os.path.join(d, "out")
    if os.path.isdir(od):
        for f in sorted(os.listdir(od)):
            if f.endswith(".json"):
                with open(os.path.join(od, f)) as fh:
                    res = json.load(fh)
    return {"rc": p.returncode, "out": out, "doc": res, "state": state or os.path.join(d, "state")}


def sims_booted(state):
    try:
        with open(os.path.join(state, "guest", "state.json")) as f:
            return [u for u, v in json.load(f)["devs"].items() if v["state"] == "Booted"]
    except OSError:
        return None


def simctl_calls(state):
    out = []
    with open(os.path.join(state, "guest", "calls.log")) as f:
        for line in f:
            out.append(json.loads(line))
    return [c for c in out if c[:2] == ["xcrun", "simctl"]]


def sections(doc, name):
    return [s for s in doc["sections"] if s["name"] == name]


CASES = {
    "clean": (["scale", "--n", "1,2,3"] + FAST, ""),
    "memfloor": (["scale", "--n", "1,2,3,4,5"] + FAST, "memfloor@3"),
    "refuse": (["scale", "--n", "1,2"] + FAST, "memfloor@0"),
    "hostmem": (["scale", "--n", "1,2,3,4,5"] + FAST, "hostmem@3"),
    "crash": (["scale", "--n", "1,2,3,4"] + FAST, "crash@2"),
    "daemoncrash": (["scale", "--n", "1,2"] + FAST, "daemoncrash@2"),
    "backwards": (["scale", "--n", "1,2,3"] + FAST, "backwards@2"),
    "kernelgpu": (["scale", "--n", "1,2,3"] + FAST, "kernelgpu@2"),
    "coherent": (["scale", "--n", "1,2,3"] + FAST, "coherent@2"),
    "slow": (["scale", "--n", "1,2,3,4"] + FAST, "slow@3"),
    "hang": (["scale", "--n", "1,2,3"] + FAST, "hang_simfps"),
    "nomarker": (["scale", "--n", "1,2"] + FAST, "nomarker"),
    "onebusy": (["scale", "--n", "1,2,3", "--mix", "one-busy"] + FAST, ""),
    "agent": (["scale", "--n", "1,2", "--mix", "agent", "--agent-period", "2"] + FAST, ""),
    "idlemix": (["scale", "--n", "1,2", "--mix", "idle"] + FAST, ""),
    "idle": (["idle", "--secs", "3", "--settle-max", "2"], ""),
    "lifecycle": (["lifecycle", "--cycles", "2", "--storm", "2", "--secs", "3"], ""),
    "build": (["build"], ""),
    "panel_skip": (["panel"], ""),
}


class Dry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            futs = {k: ex.submit(run_case, a, f) for k, (a, f) in CASES.items()}
            futs["ssh"] = ex.submit(run_case, ["scale", "--n", "1,2,3"] + FAST, "", {"BENCH_STUB_SSH_TIMEOUT": "30"})
            futs["term"] = ex.submit(run_case, ["scale", "--n", "1,2,3,4,5,6"] + FAST, "", None, 35)
            futs["deadline"] = ex.submit(run_case, ["scale", "--n", "1,2,3,4,5,6", "--deadline", "40"] + FAST, "")
            cls.r = {k: f.result() for k, f in futs.items()}

    def test_clean_sweep(self):
        r = self.r["clean"]
        self.assertEqual(r["rc"], 0, r["out"][-800:])
        d = r["doc"]
        self.assertEqual([s["n"] for s in d["data"]["steps"]], [1, 2, 3])
        self.assertEqual(d["data"]["summary"]["max_n"]["smooth60"]["max_n"], 2)
        self.assertTrue(d["publishable"], d.get("problems"))
        self.assertEqual(sims_booted(r["state"]), [])
        self.assertEqual(sections(d, "cleanup")[0]["status"], "PASS")
        for s in d["data"]["steps"]:
            self.assertEqual(len(s["sims"]), s["n"])
            for sim in s["sims"]:
                self.assertIn("input_p95_ms", sim)
                self.assertIn("guest_rss_mb", sim)

    def test_every_simctl_call_names_the_bench_set(self):
        """Every simctl call names the bench's own set; the one exception is the clone OUT of Simlock's source set (a read of
        the source, written into the bench's set), and nothing else may name Simlock's set."""
        for k in ("clean", "onebusy", "lifecycle", "agent"):
            state = self.r[k]["state"]
            bench, src = os.path.join(state, "guest", "set"), os.path.join(state, "guest", "simlock-sources")
            calls = simctl_calls(state)
            self.assertTrue(calls, k)
            clones_from_source = 0
            for c in calls:
                if c[2:4] == ["list", "devicetypes"] or c[2:4] == ["list", "runtimes"] or c[2:5] == ["list", "devices", "booted"]:
                    continue                                   # global, read only
                self.assertEqual(c[2], "--set", (k, c))
                if c[3] == src:
                    self.assertEqual((c[4], len(c), c[-1]), ("clone", 8, bench), (k, c))          # clone <source> <name> <bench set>, nothing else
                    clones_from_source += 1
                else:
                    self.assertEqual(c[3], bench, (k, c))
            self.assertEqual(clones_from_source, 1, k)           # ONE seed for the device type, whatever N is
            with open(os.path.join(state, "guest", "state.json")) as f:
                self.assertEqual(json.load(f).get("source_violations", []), [], k)

    def test_memory_floor_stops_the_sweep_and_keeps_the_data(self):
        r = self.r["memfloor"]
        d = r["doc"]
        self.assertEqual([s["n"] for s in d["data"]["steps"]], [1, 2, 3])        # the guard looks before booting: it refuses the 4th
        self.assertIn("guest available memory", d["data"]["summary"]["stopped"])
        self.assertEqual(sims_booted(r["state"]), [])
        self.assertEqual(self.r["hostmem"]["doc"]["data"]["steps"][-1]["n"], 3)
        self.assertIn("host free memory", self.r["hostmem"]["doc"]["data"]["summary"]["stopped"])

    def test_a_sweep_that_ran_no_step_fails(self):
        r = self.r["refuse"]                      # the guard refuses before the seed is even made: nothing ran
        self.assertEqual(r["doc"]["data"]["steps"], [])
        self.assertNotEqual(r["rc"], 0)
        st = sections(r["doc"], "stopped")[0]
        self.assertEqual(st["status"], "FAIL")
        self.assertIn("guest available memory", st["line"])
        self.assertEqual(sims_booted(r["state"]), [])

    def test_steps_record_whether_the_guest_settled(self):
        for st in self.r["clean"]["doc"]["data"]["steps"]:
            self.assertIn("settled", st["settle"])
            self.assertIn("waited_s", st["settle"])

    def test_run_wide_gates_catch_what_a_step_gate_does_not_name(self):
        d = self.r["crash"]["doc"]
        self.assertEqual(d["gates"]["run_crash_reports"]["status"], "FAIL")
        self.assertEqual(sections(d, "gate:run_crash_reports")[0]["status"], "FAIL")
        for k in ("clean", "lifecycle", "idle"):
            g = self.r[k]["doc"]["gates"]
            self.assertEqual(g["run_crash_reports"]["status"], "PASS", k)
            self.assertIn("run_kernel_gpu", g)

    def test_another_daemons_crash_report_is_listed_not_judged(self):
        r = self.r["daemoncrash"]
        d = r["doc"]
        self.assertEqual(r["rc"], 0, r["out"][-600:])
        self.assertEqual([st["n"] for st in d["data"]["steps"]], [1, 2])
        self.assertTrue(all(st["stable"] for st in d["data"]["steps"]))
        self.assertEqual(d["data"]["steps"][-1]["stability"]["other_ips"], ["searchd-2026-01-02-030405.ips"])
        g = d["gates"]["run_crash_reports"]
        self.assertEqual(g["status"], "PASS")
        self.assertIn("not judged: 1 other report(s): searchd-", g["detail"])

    def test_crash_report_is_instability_and_ends_the_sweep(self):
        d = self.r["crash"]["doc"]
        self.assertEqual(d["data"]["steps"][-1]["n"], 2)
        self.assertFalse(d["data"]["steps"][-1]["stable"])
        self.assertIn("instability", d["data"]["summary"]["stopped"])
        self.assertEqual(sections(d, "stability")[0]["status"], "FAIL")
        self.assertNotEqual(self.r["crash"]["rc"], 0)

    def test_strip_out_of_order_fails_the_run_but_keeps_smaller_n(self):
        r = self.r["backwards"]
        d = r["doc"]
        self.assertEqual(d["gates"]["strip"]["status"], "FAIL")
        self.assertEqual(d["data"]["steps"][0]["gates"]["strip"]["status"], "PASS")        # N=1 intact
        self.assertEqual(d["data"]["steps"][-1]["n"], 2)
        self.assertEqual(r["rc"], 1)

    def test_kernel_and_coherent_gates(self):
        k = self.r["kernelgpu"]["doc"]
        self.assertEqual(k["gates"]["kernel_gpu"]["status"], "FAIL")
        self.assertFalse(k["data"]["steps"][-1]["stable"])
        c = self.r["coherent"]["doc"]
        self.assertEqual(c["gates"]["coherent_census"]["status"], "FAIL")
        self.assertEqual(c["data"]["steps"][-1]["n"], 2)

    def test_floor_breach_stops_early(self):
        d = self.r["slow"]["doc"]
        self.assertEqual(d["data"]["steps"][-1]["n"], 3)
        self.assertIn("floor", d["data"]["summary"]["stopped"])
        self.assertEqual(d["data"]["summary"]["max_n"]["smooth60"]["max_n"], 2)

    def test_hung_observer_is_reported_not_swallowed(self):
        d = self.r["hang"]["doc"]
        self.assertTrue(d["data"]["steps"][0]["stability"]["observer_errors"])
        self.assertEqual(sims_booted(self.r["hang"]["state"]), [])

    def test_unanswered_input_probe_is_not_dropped(self):
        d = self.r["nomarker"]["doc"]
        cal = d["data"]["calibration"]
        self.assertEqual(cal["chosen"], "none")
        self.assertEqual(d["data"]["steps"][0]["targets"]["responsive"], None)       # unknown, never a pass

    def test_one_busy_mix(self):
        d = self.r["onebusy"]["doc"]
        st = d["data"]["steps"][-1]
        self.assertEqual([s["role"] for s in st["sims"]], ["busy", "static", "static"])
        self.assertIn("input_p95_ms", st["sims"][1])
        self.assertNotIn("fps", st["sims"][1])
        self.assertIsNone(st["sims"][1].get("longest_gap_s"))

    def test_agent_mix(self):
        st = self.r["agent"]["doc"]["data"]["steps"][-1]
        self.assertEqual([s["role"] for s in st["sims"]], ["agent", "agent"])
        self.assertTrue(any(s.get("actions") for s in st["sims"]), st["sims"])

    def test_idle_mix_has_no_frames_or_probes(self):
        st = self.r["idlemix"]["doc"]["data"]["steps"][-1]
        self.assertEqual({s["role"] for s in st["sims"]}, {"idle"})
        self.assertNotIn("input_p95_ms", st["sims"][0])

    def test_idle_lifecycle_build_panel(self):
        self.assertEqual(self.r["idle"]["rc"], 0, self.r["idle"]["out"][-500:])
        self.assertIn("IDLE", self.r["idle"]["doc"]["sections"][0]["line"])
        lc = self.r["lifecycle"]["doc"]
        self.assertTrue(sections(lc, "lifecycle"))
        self.assertTrue(sections(lc, "storm"))
        self.assertIn("leak_qemu_rss", lc["gates"])
        self.assertEqual(sims_booted(self.r["lifecycle"]["state"]), [])
        self.assertEqual(sections(self.r["build"]["doc"], "xcodebuild")[0]["status"], "SKIP")
        self.assertEqual(sections(self.r["panel_skip"]["doc"], "panel")[0]["status"], "SKIP")

    def test_ssh_timeout_mid_run_keeps_results_and_cleans_up(self):
        r = self.r["ssh"]
        d = r["doc"]
        self.assertTrue(d["data"]["steps"], r["out"][-600:])
        self.assertIn("ssh timed out", d["data"]["summary"]["stopped"])
        self.assertEqual(sims_booted(r["state"]), [])

    def test_sigterm_runs_the_one_trap(self):
        r = self.r["term"]
        self.assertEqual(r["rc"], 143, r["out"][-500:])
        self.assertEqual(sims_booted(r["state"]), [])
        self.assertIn("cleanup: bench simulators", r["out"])
        self.assertTrue(r["doc"] is not None and r["doc"]["data"]["steps"])

    def test_deadline_refuses_a_window_that_does_not_fit(self):
        r = self.r["deadline"]
        self.assertNotEqual(r["rc"], 0, r["out"][-500:])          # a window that does not fit before the deadline is refused
        self.assertEqual(sims_booted(r["state"]), [])
        self.assertIn("no time for", r["doc"]["data"]["summary"]["stopped"])
        self.assertEqual(r["doc"]["data"]["stopped_kind"], "error")


class KnobsAndAB(unittest.TestCase):
    def ab(self, extra, faults="", kill_after=None):
        d = tempfile.mkdtemp(prefix="dry-ab-")
        live = os.path.join(d, "live.conf")
        return d, live

    def test_ab_adopt_close_and_knob_restored(self):
        # DRY's live switch file is created by the harness; the knob must come back byte for byte
        r = run_case(["ab", "--knob", "live:group_x=1", "--n", "2", "--arm-settle", "0.2", "--secs", "3", "--settle-max", "2", "--threshold", "0.04"], "")
        d = r["doc"]
        self.assertEqual(r["rc"], 0, r["out"][-800:])
        self.assertIn(d["data"]["decision"]["decision"], ("ADOPT", "CLOSE"))
        self.assertEqual([a["arm"] for a in d["data"]["arms"]], ["A", "B", "B", "A"])
        self.assertIn("AB live:group_x=1:", d["data"]["line"])
        self.assertIn("bar 4.0%", d["data"]["line"])
        self.assertIn("rule", d["params"])
        self.assertIn("restored: live switches file", r["out"])
        live = os.path.join(r["state"], "live.conf")
        with open(live) as f:
            self.assertEqual(f.read(), "surface_coherent=1\n")
        self.assertEqual(sims_booted(r["state"]), [])

    def test_ab_sigterm_mid_arm_restores_the_knob(self):
        r = run_case(["ab", "--knob", "live:group_x=1", "--n", "2", "--arm-settle", "0.2", "--secs", "6", "--settle-max", "2"], "", None, kill_after=18)
        self.assertEqual(r["rc"], 143, r["out"][-600:])
        live = os.path.join(r["state"], "live.conf")
        with open(live) as f:
            self.assertEqual(f.read(), "surface_coherent=1\n")
        self.assertEqual(sims_booted(r["state"]), [])

    def test_ab_needs_a_knob(self):
        r = run_case(["ab", "--n", "1", "--secs", "3"], "")
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("a knob is needed", r["out"])


if __name__ == "__main__":
    unittest.main()
