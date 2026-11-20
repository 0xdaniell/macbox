"""The guards end to end in DRY: the host-disk floors (start 18, boot another 14, abort and clean up under 11), a disk that
cannot be read, a restarted VM, and the one cleanup after an abort from the monitor thread."""
import concurrent.futures
import json
import os
import unittest

from tests.test_dry import FAST, run_case, sections, sims_booted
from tests.test_dry_state import calls, files

SWEEP = ["scale", "--n", "1,2,3"] + FAST
_R = {}


def results():
    if not _R:
        cases = {"diskstart": (SWEEP, "diskstart"), "disklow": (SWEEP, "disklow@2"), "diskabort": (SWEEP, "diskabort@2"),
                 "diskunreadable": (SWEEP, "diskunreadable"), "qemugone": (SWEEP, "qemugone@2"),
                 "transition": (SWEEP, "transition@1"), "stockdisk": (["scale", "--n", "2", "--stock-devices"] + FAST[:], "disklow@2"), "clean": (SWEEP, "")}
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            futs = {k: ex.submit(run_case, a, f) for k, (a, f) in cases.items()}
            _R.update({k: f.result() for k, f in futs.items()})
    return _R


class HostDisk(unittest.TestCase):
    def test_a_run_does_not_start_under_18_gib(self):
        r = results()["diskstart"]
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("16.0 GiB is under the 18 GiB floor", sections(r["doc"], "stopped")[0]["line"])
        self.assertEqual([c for c in calls(r["state"]) if "clone" in c or "create" in c], [])      # not even a seed

    def test_another_simulator_is_not_booted_under_14_gib(self):
        r = results()["disklow"]
        self.assertEqual([s["n"] for s in r["doc"]["data"]["steps"]], [1, 2])         # with 2 booted the disk reads 12.5: the third is refused
        self.assertIn("12.5 GiB is under the 14 GiB floor to boot another simulator", r["doc"]["data"]["summary"]["stopped"])
        self.assertEqual(sims_booted(r["state"]), [])

    def test_with_stock_devices_the_guard_stands_before_each_device(self):
        r = results()["stockdisk"]
        creates = [c for c in calls(r["state"]) if "create" in c]
        self.assertEqual(len(creates), 1)                              # the second device was refused before it was made
        self.assertIn("under the 14 GiB floor", " ".join(s["line"] for s in r["doc"]["sections"] if s["name"] in ("stopped", "scale")))
        self.assertEqual(sims_booted(r["state"]), [])
        self.assertEqual(files(r["state"], "sim-keep"), [])

    def test_a_disk_that_cannot_be_read_refuses(self):
        r = results()["diskunreadable"]
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("cannot be read", r["out"])

    def test_under_11_gib_while_waiting_the_run_is_aborted_and_cleaned_up(self):
        r = results()["diskabort"]
        self.assertEqual(r["rc"], 8, r["out"][-800:])                  # the monitor thread owns the exit code
        ab = sections(r["doc"], "abort")[0]
        self.assertEqual(ab["status"], "FAIL")
        self.assertIn("9.0 GiB is under the 11 GiB floor", ab["line"])
        self.assertEqual(sections(r["doc"], "cleanup")[0]["status"], "PASS")        # verified, after the abort
        self.assertEqual(sims_booted(r["state"]), [])
        self.assertEqual(files(r["state"], "sim-keep"), [])
        self.assertEqual([s["n"] for s in r["doc"]["data"]["steps"]], [1])         # what was measured is kept

    def test_a_restarted_vm_aborts_the_run(self):
        r = results()["qemugone"]
        self.assertEqual(r["rc"], 9, r["out"][-800:])
        self.assertIn("QEMU", sections(r["doc"], "abort")[0]["line"])
        self.assertEqual(sims_booted(r["state"]), [])

    def test_a_bench_device_in_transition_is_not_a_foreign_simulator(self):
        r = results()["transition"]                          # a launchd_sim that names a bench-set device in any state is the bench's own
        self.assertEqual(r["rc"], 0, r["out"][-800:])
        self.assertEqual(sections(r["doc"], "abort") + sections(r["doc"], "foreign"), [])
        self.assertEqual([s["n"] for s in r["doc"]["data"]["steps"]], [1, 2, 3])
        for s in r["doc"]["data"]["steps"]:
            self.assertEqual(s["stability"]["foreign_booted"], 0)

    def test_an_abort_is_not_a_race_with_the_main_thread(self):
        """The monitor thread writes the result while the main thread, whose calls it killed, unwinds: the result must always be
        whole, the cleanup verified and the exit code the monitor's, every time."""
        cases = [(SWEEP, "diskabort@2", 8), (SWEEP, "qemugone@2", 9)] * 6
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            rs = list(ex.map(lambda c: (run_case(c[0], c[1]), c[2]), cases))
        for r, code in rs:
            self.assertEqual(r["rc"], code, r["out"][-600:])
            self.assertIsNotNone(r["doc"], r["out"][-600:])
            self.assertEqual(sections(r["doc"], "cleanup")[0]["status"], "PASS")
            self.assertEqual([s["name"] for s in r["doc"]["sections"]].count("abort"), 1)
            self.assertEqual([s["name"] for s in r["doc"]["sections"] if s["name"] in ("stopped", "error")], [])

    def test_a_clean_run_is_not_aborted(self):
        r = results()["clean"]
        self.assertEqual(r["rc"], 0, r["out"][-500:])
        self.assertEqual(sections(r["doc"], "abort"), [])


if __name__ == "__main__":
    unittest.main()
