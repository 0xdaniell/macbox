"""The guards (host disk floors, guest swap and compressor, the VM still the same and answering, someone else's simulator) and
the monitor that applies them while the run waits. Pure: fake host, fake guest, no VM."""
import argparse
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from macbench import cli, core, hostprobe  # noqa: E402


class FakeHost:
    dry = True

    def __init__(self, disk=500.0, same=True, mem=40.0):
        self.disk, self.same, self.mem = disk, same, mem

    def mem_avail_gb(self):
        return self.mem

    def disk_free_gb(self, path):
        return self.disk

    def same_qemu(self):
        return self.same


class FakeGuest:
    def __init__(self, mem=None, foreign=0, alive=True, pressure=1):
        self.mem = mem or {"avail_mb": 9000, "free_mb": 5000, "swapouts": 7, "compressed_mb": 200}
        self.foreign, self.up, self.pressure, self.calls = foreign, alive, pressure, []

    def call(self, args, timeout=60, counted=True):
        self.calls.append(args[0])
        return {"mem": self.mem, "foreign_booted": self.foreign, "pressure": self.pressure, "booted": 0}

    quick = call

    def alive(self):
        self.calls.append("alive")
        return self.up


def ns(**kw):
    a = cli.parser().parse_args(["scale"])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class Ctx:
    def __init__(self, host=None, guest=None, **kw):
        self.a = ns(**kw)
        self.host, self.guest = host or FakeHost(), guest or FakeGuest()
        self.est_sim_mb, self.swap0, self.in_window, self.taken = 1100.0, 7, False, True


class DiskFloors(unittest.TestCase):
    def test_the_defaults_are_the_outage_lessons(self):
        a = ns()
        self.assertEqual((a.disk_start_gib, a.disk_boot_gib, a.disk_abort_gib, a.max_compressor_mb), (18.0, 14.0, 11.0, 2048.0))

    def test_start_boot_and_abort_floors(self):
        for disk, start, boot, keep_going in ((20, True, True, True), (17.9, False, True, True), (13.9, False, False, True), (10.9, False, False, False)):
            c = Ctx(host=FakeHost(disk=disk))
            self.assertEqual(core.start_reason(c) is None, start, disk)
            self.assertEqual(core.guard(c, 1, "x") is None, boot, disk)
            self.assertEqual(core.disk_reason(c, c.a.disk_abort_gib, "go on") is None, keep_going, disk)

    def test_an_unreadable_disk_refuses_everything(self):
        c = Ctx(host=FakeHost(disk=None))
        self.assertIn("cannot be read", core.start_reason(c))
        self.assertIn("cannot be read", core.guard(c, 1, "x"))
        self.assertIn("cannot be read", monitor_result(c)[0])
        self.assertEqual(monitor_result(c)[1], 8)


class GuestGuards(unittest.TestCase):
    def test_swapouts_may_not_grow_and_the_compressor_is_capped(self):
        self.assertIsNone(core.guard(Ctx(), 1, "x"))
        c = Ctx(guest=FakeGuest(mem={"avail_mb": 9000, "free_mb": 5000, "swapouts": 8, "compressed_mb": 200}))
        self.assertIn("swap-outs grew from 7 to 8", core.guard(c, 1, "x"))
        c = Ctx(guest=FakeGuest(mem={"avail_mb": 9000, "free_mb": 5000, "swapouts": 7, "compressed_mb": 2100}))
        self.assertIn("compressor holds 2100 MB", core.guard(c, 1, "x"))

    def test_someone_elses_simulator_refuses_a_boot_unless_allowed(self):
        self.assertIn("outside the bench", core.guard(Ctx(guest=FakeGuest(foreign=1)), 1, "x"))
        self.assertIsNone(core.guard(Ctx(guest=FakeGuest(foreign=1), allow_foreign=True), 1, "x"))


def monitor_result(ctx):
    ctx.a.watch_guest_every = 0.0
    return core.monitor_check(ctx)()


class Monitor(unittest.TestCase):
    def test_quiet_run(self):
        self.assertIsNone(monitor_result(Ctx()))

    def test_disk_under_the_abort_floor_ends_the_run_with_8(self):
        r = monitor_result(Ctx(host=FakeHost(disk=10.5)))
        self.assertEqual(r[1], 8)
        self.assertIn("under the 11 GiB floor", r[0])

    def test_a_restarted_qemu_ends_the_run_with_9(self):
        self.assertEqual(monitor_result(Ctx(host=FakeHost(same=False)))[1], 9)

    def test_a_vm_that_stops_answering_twice_in_a_row_ends_the_run_with_9(self):
        c = Ctx(guest=FakeGuest(alive=False))
        check = core.monitor_check(c)
        c.a.watch_guest_every = 0.0
        self.assertIsNone(check())                  # one miss is not a verdict
        self.assertEqual(check()[1], 9)
        c = Ctx(guest=FakeGuest(alive=False))
        c.a.watch_guest_every = 0.0
        check = core.monitor_check(c)
        check()
        c.guest.up = True
        self.assertIsNone(check())                  # an answer resets the count
        c.guest.up = False
        self.assertIsNone(check())

    def test_someone_elses_simulator_makes_the_run_yield_with_7(self):
        self.assertEqual(monitor_result(Ctx(guest=FakeGuest(foreign=1)))[1], 7)
        self.assertIsNone(monitor_result(Ctx(guest=FakeGuest(foreign=1), allow_foreign=True)))

    def test_swap_growth_and_the_compressor_end_the_run_with_10(self):
        self.assertEqual(monitor_result(Ctx(guest=FakeGuest(mem={"swapouts": 9, "compressed_mb": 1})))[1], 10)
        self.assertEqual(monitor_result(Ctx(guest=FakeGuest(mem={"swapouts": 7, "compressed_mb": 9999})))[1], 10)

    def test_a_measurement_window_is_left_alone(self):
        c = Ctx(guest=FakeGuest(foreign=1))
        c.in_window = True
        self.assertIsNone(monitor_result(c))
        self.assertEqual(c.guest.calls, [])         # no observer load: the guest was not asked
        c.host.disk = 5
        self.assertEqual(monitor_result(c)[1], 8)   # but the host-side checks go on


class QemuIdentity(unittest.TestCase):
    def test_reads_a_real_process(self):
        h = hostprobe.Host(os.getpid())
        self.assertIsNotNone(h.born)
        self.assertTrue(h.same_qemu())

    def test_same_pid_with_another_start_time_is_another_vm(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "proc", "42"))

        def stat(start):
            with open(os.path.join(root, "proc", "42", "stat"), "w") as f:
                f.write("42 (qemu-system-x86) S " + " ".join(["1"] * 18) + " %s 5\n" % start)
        stat(1000)
        h = hostprobe.Host(42, root=root, hz=100)
        self.assertTrue(h.same_qemu())
        stat(1000)
        self.assertTrue(h.same_qemu())
        stat(7777)
        self.assertFalse(h.same_qemu())
        os.remove(os.path.join(root, "proc", "42", "stat"))
        self.assertFalse(h.same_qemu())


SCRIPT = r'''
import sys, time, threading
sys.path.insert(0, %r)
from macbench.runctx import Run
r = Run(60, print, margin=2)
r.teardown.append(("t", lambda: (time.sleep(0.5), print("TEARDOWN RAN", flush=True))))
r.on_abort = lambda reason: print("ABORT RECORDED", reason, flush=True)
r.on_deadline = lambda reason=None: print("PARTIAL WRITTEN", reason, flush=True)
r.final = lambda: print("FINAL", flush=True)
r.install()
t0 = time.time()
r.watch("x", lambda: ("disk gone", 8) if time.time() - t0 > 0.3 else None, 0.1)
try:
    time.sleep(30)             # the main thread is blocked in a "call"
finally:
    r.cleanup()                # and runs the one cleanup too when it is woken: it must wait for the monitor's
    r.join_abort()
'''


class AbortFromAMonitor(unittest.TestCase):
    def test_partial_result_cleanup_then_exit_code(self):
        t0 = time.time()
        p = subprocess.run([sys.executable, "-c", SCRIPT % os.path.join(HERE, "..")], capture_output=True, text=True, timeout=40)
        self.assertEqual(p.returncode, 8, p.stdout + p.stderr)
        out = p.stdout
        self.assertLess(time.time() - t0, 20)
        for word in ("disk gone", "PARTIAL WRITTEN", "TEARDOWN RAN", "FINAL"):
            self.assertIn(word, out)
        self.assertLess(out.index("TEARDOWN RAN"), out.index("FINAL"))      # the closing sections come after the cleanup


if __name__ == "__main__":
    unittest.main()
