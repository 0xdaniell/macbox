"""The production seed, in DRY: made from a Simlock source device once, reused, never touching Simlock's set; every bench
simulator a clone of it with the seed's Slim disables; the explicit stock path; every way a seed can fail."""
import json
import os
import shutil
import tempfile
import unittest

from guest import benchguest_stub as stub
from tests.test_dry import FAST, run_case, sections
from tests.test_dry_state import calls, files

ONE = ["scale", "--n", "1,2"] + FAST
_CACHE = {}


def parallel(jobs):
    """Runs {name: fn} in threads once and caches the results: every DRY run is its own process, they do not share anything
    but their own state directory."""
    import concurrent.futures
    todo = {k: f for k, f in jobs.items() if k not in _CACHE}
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        futs = {k: ex.submit(f) for k, f in todo.items()}
        for k, f in futs.items():
            _CACHE[k] = f.result()
    return {k: _CACHE[k] for k in jobs}


def verbs(state, word):
    return [c for c in calls(state) if word in c]


def devs(state):
    with open(os.path.join(state, "guest", "state.json")) as f:
        return json.load(f)["devs"]


def manifest(state):
    with open(os.path.join(state, "guest", "set", ".bench-manifest.json")) as f:
        return json.load(f)


def new_state():
    return tempfile.mkdtemp(prefix="dry-seed-")


def job_seed2():
    st = new_state()
    r1 = run_case(ONE, "", state=st)
    n1 = len([c for c in calls(st) if c[0].endswith("simslim")])
    return {"state": st, "r1": r1, "n_slim1": n1, "r2": run_case(ONE, "", state=st)}


def job_fail(fault, args=None):
    def f():
        st = new_state()
        return {"state": st, "r": run_case(args or ONE, fault, state=st), "next": None}
    return f


def job_fail_then_ok(fault, args=None):
    def f():
        st = new_state()
        r = run_case(args or ONE, fault, state=st)
        snap = {"devs": devs(st), "keep": files(st, "sim-keep")}
        return {"state": st, "r": r, "snap": snap, "next": run_case(ONE, "", state=st)}
    return f


def job_stale():
    st = new_state()
    out = {"state": st, "first": run_case(ONE, "", state=st)}
    p = os.path.join(st, "guest", "set", ".bench-manifest.json")
    m = manifest(st)
    for t in m["seeds"].values():
        t["app_stamp"] = "stale"
    with open(p, "w") as f:
        json.dump(m, f)
    out["stale"] = run_case(ONE, "", state=st)
    out["rebuilt"] = run_case(ONE + ["--rebuild-seed"], "", state=st)
    return out


def job_seedcmd():
    st = new_state()
    out = {"state": st, "seed": run_case(["seed", "--secs", "3"], "", state=st)}
    out["ndevs"] = len(devs(st))
    out["cleanup"] = run_case(["cleanup", "--seeds"], "", state=st)
    return out


JOBS = {
    "seed2": job_seed2,
    "nosource": job_fail_then_ok("nosource"),
    "nosourceset": job_fail("nosourceset"),
    "slimfail": job_fail_then_ok("slimfail"),
    "slimfew": job_fail_then_ok("slimfew"),
    "noicons": lambda: (lambda st: {"state": st, "r": run_case(ONE, "noicons", state=st)})(new_state()),
    "noinstall": job_fail_then_ok("noinstall"),
    "stale": job_stale,
    "seedcmd": job_seedcmd,
    "stock": lambda: (lambda st: {"state": st, "r": run_case(ONE + ["--stock-devices"], "nosourceset", state=st)})(new_state()),
    "lifecycle": lambda: (lambda st: {"state": st, "r": run_case(["lifecycle", "--cycles", "2", "--storm", "2", "--secs", "3"], "", state=st)})(new_state()),
}


class Seed(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = parallel(JOBS)["seed2"]
        cls.state, cls.r1, cls.r2, cls.n_slim1 = d["state"], d["r1"], d["r2"], d["n_slim1"]

    def test_the_seed_is_made_once_from_the_simlock_source_then_reused(self):
        self.assertEqual((self.r1["rc"], self.r2["rc"]), (0, 0), self.r1["out"][-500:] + self.r2["out"][-500:])
        self.assertEqual(self.n_slim1, 1)
        self.assertEqual(len([c for c in calls(self.state) if c[0].endswith("simslim")]), 1)         # the second run did not make another
        src = os.path.join(self.state, "guest", "simlock-sources")
        from_source = [c for c in calls(self.state) if c[:4] == ["xcrun", "simctl", "--set", src]]
        self.assertEqual(len(from_source), 1)
        self.assertEqual(from_source[0][4:6], ["clone", "5A5A5A5A-0000-4000-8000-000000000003"])        # the 1x type's source
        self.assertIn("made from a Simlock source device", sections(self.r1["doc"], "seed")[0]["line"])
        self.assertIn("reused", sections(self.r2["doc"], "seed")[0]["line"])
        self.assertFalse(self.r1["doc"]["env"]["guest"]["seed"]["reused"])
        self.assertTrue(self.r2["doc"]["env"]["guest"]["seed"]["reused"])

    def test_the_seed_is_kept_shut_down_and_everything_else_is_gone(self):
        d = devs(self.state)
        self.assertEqual([v["name"] for v in d.values()], ["bench-seed-iPhone-17-Pro-1x"])
        self.assertEqual({v["state"] for v in d.values()}, {"Shutdown"})
        self.assertEqual(manifest(self.state)["seeds"]["dev.macbox.CoreSimulator.SimDeviceType.iPhone-17-Pro-1x"]["complete"], True)
        self.assertEqual(files(self.state, "sim-keep"), [])
        ov = files(self.state, "overrides")                        # only the seed's own Slim disables stay
        self.assertEqual(len(ov), 1)
        self.assertEqual(ov[0].split(".")[-1], list(d)[0])

    def test_simlocks_set_is_read_only(self):
        src = os.path.join(self.state, "guest", "simlock-sources")
        fresh = tempfile.mkdtemp(prefix="dry-src-")
        stub.make_source_set(os.path.join(fresh, "s"))

        def tree(root):
            out = []
            for dp, _, fs in os.walk(root):
                for f in fs:
                    with open(os.path.join(dp, f), "rb") as fh:
                        out.append((os.path.relpath(os.path.join(dp, f), root), fh.read()))
            return sorted(out)
        self.assertEqual(tree(src), tree(os.path.join(fresh, "s")))
        with open(os.path.join(self.state, "guest", "state.json")) as f:
            self.assertEqual(json.load(f).get("source_violations", []), [])
        for c in calls(self.state):
            self.assertNotIn("config.json", " ".join(c))
            if c[:2] == ["xcrun", "simctl"] and c[3] == src:
                self.assertEqual(c[4], "clone")                      # never boot, shutdown, delete, create... in it

    def test_every_clone_has_the_seeds_slim_disables_before_its_first_boot(self):
        seed = list(devs(self.state))[0]
        cs = calls(self.state)
        boots = [c[-1] for c in cs if c[:2] == ["xcrun", "simctl"] and c[4:5] == ["boot"]]
        clone_udids = [u for u in boots if u != seed]
        self.assertTrue(clone_udids)
        for u in clone_udids:
            boot_at = next(i for i, c in enumerate(cs) if c[4:6] == ["boot", u])
            checked = [i for i, c in enumerate(cs) if c[0] == "plutil" and c[-1].endswith("SimDevice.%s/disabled.plist" % u)]
            self.assertTrue(checked and checked[0] < boot_at, u)
        for st in self.r1["doc"]["data"]["steps"]:
            self.assertEqual(len(st["sims"]), st["n"])

    def test_the_seed_is_slimmed_after_one_boot_and_before_the_app(self):
        cs = calls(self.state)
        kinds = []
        for c in cs:
            if c[0].endswith("simslim"):
                kinds.append("slim")
            elif c[:2] == ["xcrun", "simctl"] and c[4:5] == ["boot"]:
                kinds.append("boot")
            elif c[:2] == ["xcrun", "simctl"] and c[4:5] == ["install"]:
                kinds.append("install")
        self.assertEqual(kinds[:3], ["boot", "slim", "install"])
        slim = next(c for c in cs if c[0].endswith("simslim"))
        self.assertEqual(slim[1:4], ["--set", os.path.join(self.state, "guest", "set"), "on"])
        self.assertIn("--profile", slim)


class SeedFailures(unittest.TestCase):
    def failing(self, key, needle):
        d = parallel(JOBS)[key]
        r = d["r"]
        self.assertNotEqual(r["rc"], 0, r["out"][-500:])
        self.assertIn(needle, r["out"])
        self.assertEqual(d["snap"]["devs"], {}, "a failed seed must not stay in the set")             # deleted again: never reusable
        self.assertEqual(d["snap"]["keep"], [])
        self.assertEqual(r["doc"]["data"]["steps"], [])
        self.assertEqual(d["next"]["rc"], 0, d["next"]["out"][-500:])                                   # and the next run is not blocked
        return r

    def test_no_source_device_of_the_type_says_so_and_offers_the_stock_path(self):
        r = self.failing("nosource", "Simlock has no source device of type iPhone-17-Pro-1x (it has: iPhone-17)")
        self.assertIn("--stock-devices", r["out"])
        self.assertEqual([c for c in calls(r["state"]) if "create" in c], [])             # it did not fall back to stock devices by itself

    def test_no_simlock_at_all_says_so(self):
        r = parallel(JOBS)["nosourceset"]["r"]
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("Simlock is optional", r["out"])
        self.assertIn("--stock-devices", r["out"])

    def test_simslim_failure(self):
        self.failing("slimfail", "simslim failed on the seed")

    def test_a_seed_that_is_not_slim_enough_fails(self):
        self.failing("slimfew", "not Slim after simslim: 20 services off")

    def test_a_seed_without_the_widgets_icons_folder_is_recorded_not_refused(self):
        r = parallel(JOBS)["noicons"]["r"]                 # the folder is not in the verified recipe: an unknown, never a gate
        self.assertEqual(r["rc"], 0, r["out"][-500:])
        self.assertIs(r["doc"]["env"]["guest"]["seed"]["icons"], False)

    def test_the_app_must_be_in_the_seed(self):
        self.failing("noinstall", "not installed in the seed")


class SeedLifecycle(unittest.TestCase):
    def test_a_stale_app_stops_the_run_and_rebuild_seed_fixes_it(self):
        d = parallel(JOBS)["stale"]
        self.assertEqual(d["first"]["rc"], 0)
        self.assertNotEqual(d["stale"]["rc"], 0)
        self.assertIn("--rebuild-seed", d["stale"]["out"])
        self.assertEqual(d["rebuilt"]["rc"], 0, d["rebuilt"]["out"][-500:])
        self.assertEqual(len([c for c in calls(d["state"]) if c[0].endswith("simslim")]), 2)
        self.assertFalse(d["rebuilt"]["doc"]["env"]["guest"]["seed"]["reused"])

    def test_the_seed_command_and_cleanup_seeds(self):
        d = parallel(JOBS)["seedcmd"]
        self.assertEqual(d["seed"]["rc"], 0, d["seed"]["out"][-500:])
        self.assertEqual(d["ndevs"], 1)
        self.assertEqual(d["cleanup"]["rc"], 0, d["cleanup"]["out"])
        self.assertEqual(devs(d["state"]), {})
        self.assertEqual(files(d["state"], "overrides"), [])

    def test_stock_devices_are_an_explicit_path_and_say_so(self):
        d = parallel(JOBS)["stock"]
        r, state = d["r"], d["state"]                                           # needs no Simlock at all (nosourceset)
        self.assertEqual(r["rc"], 0, r["out"][-600:])
        cs = calls(state)
        self.assertEqual(len([c for c in cs if "create" in c]), 2)
        self.assertEqual([c for c in cs if c[0].endswith("simslim")], [])
        self.assertIn("fresh stock devices", r["doc"]["env"]["guest"]["device_type"])
        self.assertEqual(r["doc"]["env"]["guest"]["device_source"], "stock devices")
        self.assertEqual(devs(state), {})
        self.assertEqual(files(state, "sim-keep"), [])

    def test_the_lifecycle_cycles_clone_the_seed(self):
        d = parallel(JOBS)["lifecycle"]
        r, state = d["r"], d["state"]
        self.assertEqual(r["rc"], 0, r["out"][-600:])
        med = r["doc"]["data"]["median_s"]
        self.assertIn("clone_s", med)
        self.assertNotIn("create_s", med)
        self.assertNotIn("install_s", med)
        self.assertEqual([c for c in calls(state) if "create" in c], [])
        self.assertEqual(len(devs(state)), 1)


if __name__ == "__main__":
    unittest.main()
