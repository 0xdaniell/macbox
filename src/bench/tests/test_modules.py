import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from macbench import analysis, chart, compare, gates, hostprobe, schema  # noqa: E402
from tests import helpers  # noqa: E402

spec = importlib.util.spec_from_file_location("benchguest", os.path.join(HERE, "..", "guest", "benchguest.py"))
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               573102.
Pages active:                            1833909.
Pages inactive:                           100000.
Pages speculative:                         10000.
Pages wired down:                         946936.
Pages occupied by compressor:              52949.
Pageins:                                  123456.
Pageouts:                                     12.
Swapins:                                       0.
Swapouts:                                      7.
"""
PS = """    1     0   4000   0:01.50 /sbin/launchd
  200     1  20000   2:03.25 launchd_sim /x/set/AAAA-1/data
  201   200 150000 1-00:00:10.00 SpringBoard
  300     1  90000   0:10.00 SimMetalHost -d BBBB-2
  301   300  1000    0:00.10 helper
  400     1  1000    0:00.50 unrelated AAAA-1x
"""


def wr(path, mode, text):
    with open(path, mode) as f:
        f.write(text)


class SlimProfile(unittest.TestCase):
    def test_explicit_env_then_the_only_file_else_an_error_that_says_what_to_pass(self):
        import tempfile
        d = tempfile.mkdtemp()
        old = (bg.SLIM_DIR, bg.SLIM_PROFILE)
        try:
            bg.SLIM_DIR, bg.SLIM_PROFILE = d, None
            with self.assertRaises(bg.GuestError) as e:
                bg.resolve_profile()
            self.assertIn("--slim-profile", str(e.exception))
            wr(os.path.join(d, "a.json"), "w", "{}")
            self.assertEqual(bg.resolve_profile(), os.path.join(d, "a.json"))
            wr(os.path.join(d, "b.json"), "w", "{}")
            with self.assertRaises(bg.GuestError):
                bg.resolve_profile()                                  # two: no guessing
            self.assertEqual(bg.resolve_profile("/x/p.json"), "/x/p.json")
            bg.SLIM_PROFILE = "/env/p.json"
            self.assertEqual(bg.resolve_profile(), "/env/p.json")
        finally:
            bg.SLIM_DIR, bg.SLIM_PROFILE = old


class GuestParsers(unittest.TestCase):
    def test_vm_stat(self):
        m = bg.mem_summary(bg.parse_vm_stat(VM_STAT))
        self.assertEqual(m["swapouts"], 7)
        self.assertAlmostEqual(m["free_mb"], (573102 + 10000) * 16384 / 1048576.0, places=1)
        self.assertAlmostEqual(m["compressed_mb"], 52949 * 16384 / 1048576.0, places=1)
        self.assertEqual(bg.parse_vm_stat(VM_STAT)["page"], 16384)

    def test_available_memory_counts_cache_and_defaults_to_4k_pages(self):
        m = bg.mem_summary(bg.parse_vm_stat(VM_STAT))
        self.assertAlmostEqual(m["avail_mb"], (573102 + 10000 + 100000) * 16384 / 1048576.0, places=1)    # + inactive
        self.assertGreater(m["avail_mb"], m["free_mb"])
        nohead = bg.parse_vm_stat(VM_STAT.replace("(page size of 16384 bytes)", ""))
        if os.uname()[4] == "x86_64":
            # without the header (x86 guest): 573102 free pages are 2.2 GiB at 4 KiB, not 8.7 GiB
            self.assertEqual(nohead["page"], 4096)
            self.assertAlmostEqual(573102 * 4096 / 1073741824.0, 2.19, places=2)

    def test_observer_is_nobodys_simulator_tree(self):
        procs = bg.parse_ps(PS + "  500     1  2000    0:03.00 xcrun simctl --set /x AAAA-1 BBBB-2\n  501   500  2000    0:09.00 simfps AAAA-1\n")
        own = bg.own_pids(procs, me=501)
        self.assertEqual(own, {500, 501})
        trees = bg.sim_trees(procs, ["AAAA-1", "BBBB-2"], own)
        self.assertNotIn(500, trees["AAAA-1"] | trees["BBBB-2"])
        self.assertNotIn(501, trees["AAAA-1"])
        later = [dict(p, cpu_s=p["cpu_s"] + 1.0) for p in procs]
        per, tot = bg.attribute(procs, later, ["AAAA-1", "BBBB-2"], 2.0, own)
        self.assertAlmostEqual(tot["observer_cpu_pct"], 100.0, places=1)       # two processes x 1 s over 2 s
        self.assertEqual(per["AAAA-1"]["procs"], 3)

    def test_settle_ignores_the_load_by_whole_command(self):
        for cmd in ("/Library/Developer/CoreSimulator/Volumes/iOS 26.5.simruntime/Contents/bin/backboardd",
                    "/x/SimMetalHost -d AAAA-1", "simfps -set /x", "/usr/bin/ssh host", "/usr/bin/python3 benchguest.py settle"):
            self.assertTrue(bg.SETTLE_SKIP.search(cmd), cmd)
        for cmd in ("/usr/libexec/mds", "/System/Library/Frameworks/Foo.framework/assetsd", "/usr/sbin/cfprefsd daemon"):
            self.assertFalse(bg.SETTLE_SKIP.search(cmd), cmd)

    def test_count_launchd_sim_sees_every_device_set(self):
        text = PS + "  600     1  20000   0:01.00 /Library/x/launchd_sim /other/set/CCCC-3/data\n"
        saved = bg.ps_text
        bg.ps_text = lambda: text
        try:
            self.assertEqual(bg.count_launchd_sim(), 2)
        finally:
            bg.ps_text = saved

    def test_qemu_env_value_reads_the_process_environ(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "proc", "77"))
            wr(os.path.join(root, "proc", "77", "environ"), "w", "A=1\0REIMS_VGPU_CENSUS=/run/x/coherent\0REIMS_VGPU_COHERENT=1\0")
            self.assertEqual(hostprobe.qemu_env_value(77, "REIMS_VGPU_CENSUS", root), "/run/x/coherent")
            self.assertIsNone(hostprobe.qemu_env_value(77, "NOPE", root))
            self.assertIsNone(hostprobe.qemu_env_value(78, "A", root))

    def test_cputime(self):
        self.assertAlmostEqual(bg.parse_cputime("0:01.50"), 1.5)
        self.assertAlmostEqual(bg.parse_cputime("2:03.25"), 123.25)
        self.assertAlmostEqual(bg.parse_cputime("1-00:00:10.00"), 86410.0)

    def test_process_trees_and_attribution(self):
        procs = bg.parse_ps(PS)
        trees = bg.sim_trees(procs, ["AAAA-1", "BBBB-2"])
        self.assertEqual(trees["AAAA-1"], {200, 201, 400})      # a command line that names it, and children
        self.assertEqual(trees["BBBB-2"], {300, 301})
        later = [dict(p, cpu_s=p["cpu_s"] + 2.0) for p in procs]
        per, tot = bg.attribute(procs, later, ["AAAA-1", "BBBB-2"], 4.0)
        self.assertEqual(per["BBBB-2"]["procs"], 2)
        self.assertAlmostEqual(per["BBBB-2"]["cpu_pct"], 100.0, places=1)       # 2 processes x 2 s over 4 s
        self.assertAlmostEqual(tot["total_cpu_pct"], 6 * 2.0 / 4 * 100, places=1)

    def test_pace(self):
        recs = bg.parse_pace('{"at": 100.5, "counter": 300, "ticks": 300, "fps": 59.9}\nnot json\n{"at": 105.5, "counter": 600, "ticks": 300, "fps": 60.0}\n')
        self.assertEqual(recs, [(100.5, 300, 59.9, 300), (105.5, 600, 60.0, 300)])
        ps = analysis.pace_summary(recs + [(110.5, 900, 60.1, 300)], 102.0, 108.0, 101.0, 109.0)
        self.assertEqual(ps["app_ticks"], 600)           # records at or before the first frame (300) to the first after the last (900)
        self.assertAlmostEqual(ps["app_tick_fps"], 60.0, delta=0.2)


class HostParsers(unittest.TestCase):
    def test_task_stat_with_awkward_names(self):
        fields = ["S", "1", "1", "0", "0", "0", "0", "0", "0", "0", "0", "300", "50", "0", "0", "20", "0", "1", "0", "100", "0", "0"] + ["0"] * 19 + ["120", "0"]
        text = "123 (CPU 3/KVM (x)) " + " ".join(fields)
        comm, ut, st, gt = hostprobe.parse_task_stat(text)
        self.assertEqual((comm, ut, st), ("CPU 3/KVM (x)", 300, 50))
        self.assertEqual(gt, int(fields[40]))

    def test_thread_delta_splits_user_guest_sys(self):
        a = {"1": ("CPU 0/KVM", 1000, 100, 800), "2": ("CPU 1/KVM", 1000, 100, 800), "3": ("metal2vulkan-sc", 100, 10, 0)}
        b = {"1": ("CPU 0/KVM", 1400, 120, 1100), "2": ("CPU 1/KVM", 1400, 120, 1100), "3": ("metal2vulkan-sc", 150, 30, 0)}
        d = hostprobe.thread_delta(a, b, 2.0, hz=100)
        # user (host side) = (400-300) + (400-300) + 50 = 250 ticks; guest = 600; sys = 20+20+20 = 60: over 2 s at 100 Hz
        self.assertAlmostEqual(d["user"], 1.25)
        self.assertAlmostEqual(d["guest"], 3.0)
        self.assertAlmostEqual(d["sys"], 0.3)
        self.assertAlmostEqual(d["cores"], 4.55)
        self.assertEqual(d["top"][0]["name"], "vCPU")
        self.assertEqual(d["top"][0]["threads"], 2)

    def test_fdinfo_and_fake_proc_tree(self):
        with tempfile.TemporaryDirectory() as root:
            def put(rel, text):
                p = root + rel
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(text)
            put("/proc/7/fdinfo/10", "pos: 0\ndrm-client-id:\t5\ndrm-engine-render:\t1000 ns\n")
            put("/proc/7/fdinfo/11", "pos: 0\ndrm-client-id:\t5\ndrm-engine-render:\t1000 ns\n")      # a dup of the same client
            put("/proc/7/fdinfo/12", "pos: 0\ndrm-client-id:\t6\ndrm-engine-render:\t500 ns\n")
            put("/proc/7/fdinfo/13", "pos: 0\nflags: 02\n")
            self.assertEqual(hostprobe.gpu_render_ns(7, root), 1500)
            self.assertIsNone(hostprobe.gpu_render_ns(8, root))
            put("/proc/7/cgroup", "0::/machine/vm.scope\n")
            put("/sys/fs/cgroup/machine/vm.scope/memory.current", "4294967296\n")
            put("/sys/fs/cgroup/machine/vm.scope/memory.swap.current", "1048576\n")
            put("/sys/fs/cgroup/machine/vm.scope/memory.events", "low 0\nhigh 0\nmax 2\noom 0\noom_kill 0\n")
            put("/proc/meminfo", "MemTotal:       1000000 kB\nMemAvailable:    500000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
            put("/proc/vmstat", "pswpin 10\npswpout 20\n")
            put("/proc/7/status", "VmRSS:\t  2048 kB\n")
            a = hostprobe.mem_snapshot(7, root)
            self.assertEqual(a["cg_current"], 4294967296)
            self.assertEqual(a["cg_swap"], 1048576)
            self.assertEqual(a["qemu_rss_kb"], 2048)
            self.assertEqual((a["mem_avail_kb"], a["mem_total_kb"]), (500000, 1000000))
            put("/proc/7/maps", "7f00 r-xp 0 00:00 0 /opt/mesa/lib/libvulkan_intel.so\n")
            self.assertTrue(hostprobe.mesa_is_custom(7, root))
            put("/proc/7/maps", "7f00 r-xp 0 00:00 0 /usr/lib/x86_64-linux-gnu/libvulkan_intel.so\n")
            self.assertFalse(hostprobe.mesa_is_custom(7, root))
            put("/proc/vmstat", "pswpin 15\npswpout 120\n")
            put("/sys/fs/cgroup/machine/vm.scope/memory.swap.current", "3145728\n")
            b = hostprobe.mem_snapshot(7, root)
            d = hostprobe.mem_delta(a, b, 10)
            self.assertEqual((d["pswpin_pages"], d["pswpout_pages"]), (5, 100))
            self.assertAlmostEqual(d["cgroup_swap_delta_mb"], 2.0)
            put("/proc/7/environ", "A=1\0REIMS_VGPU_CENSUS=/some/dir\0REIMS_VGPU_COHERENT=1\0")
            self.assertEqual(hostprobe.qemu_env_switches(7, root), {"REIMS_VGPU_CENSUS": "<dir>", "REIMS_VGPU_COHERENT": "1"})

    def test_gpu_error_regex(self):
        self.assertTrue(hostprobe.GPU_ERRORS.search("i915 0000:00:02.0: [drm] GPU HANG: ecode 12"))
        self.assertTrue(hostprobe.GPU_ERRORS.search("Fence expired on rcs0"))
        self.assertFalse(hostprobe.GPU_ERRORS.search("i915: frequency changed to 1200"))


class Gates(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.census = os.path.join(self.d, "coherent.jsonl")
        self.log = os.path.join(self.d, "qemu.log")
        self.hang = os.path.join(self.d, "hang")
        wr(self.log, "w", "old line\ncommand buffer failed (before the mark)\n")
        with open(self.census, "w") as f:
            f.write(json.dumps({"id": 1, "overwrite_risk": 3, "sync_errors": 0}) + "\n")      # history: before the mark
            f.write(json.dumps({"process": 1, "late_copies_in_busy": 0}) + "\n")

    def test_deltas_only(self):
        c = gates.Coherent(self.d, self.log, self.hang)
        m = c.mark()
        r = c.check(m)
        self.assertEqual(r["coherent_census"]["status"], "SKIP")        # nothing new: not a pass
        self.assertEqual(r["qemu_log"]["status"], "PASS")               # the old failure is before the mark
        with open(self.census, "a") as f:
            f.write(json.dumps({"id": 1, "overwrite_risk": 0, "sync_errors": 0}) + "\n")
        self.assertEqual(c.check(m)["coherent_census"]["status"], "PASS")
        with open(self.census, "a") as f:
            f.write(json.dumps({"id": 1, "overwrite_risk": 1, "sync_errors": 0}) + "\n")
            f.write(json.dumps({"process": 1, "late_copies_in_busy": 2}) + "\n")
        r = c.check(m)
        self.assertEqual(r["coherent_census"]["status"], "FAIL")
        self.assertEqual(r["coherent_process"]["status"], "FAIL")
        wr(self.log, "a", "m2v: command buffer failed\n")
        wr(self.hang, "w", "x")
        r = c.check(m)
        self.assertEqual(r["qemu_log"]["status"], "FAIL")
        self.assertEqual(r["gpu_hang"]["status"], "FAIL")

    def test_unreadable_is_skip_never_pass(self):
        r = gates.Coherent(None, None, None).check({"n": 0, "proc": {k: 0 for k in gates.PROCESS_GATES}, "log_off": None, "hang": False})
        self.assertTrue(all(g["status"] == "SKIP" for g in r.values()), r)
        self.assertEqual(gates.kernel_gate(None, "no journal")["status"], "SKIP")
        self.assertEqual(gates.kernel_gate(0, "")["status"], "PASS")
        self.assertEqual(gates.kernel_gate(1, "x")["status"], "FAIL")

    def test_screen_gates(self):
        ok = {"ok": True, "hash": "aa", "tear": False, "tear_applicable": True, "w": 402, "h": 874}
        by = [{"idx": 0, "role": "busy", "shots": [ok, dict(ok, tear=True)]}, {"idx": 1, "role": "static", "shots": [ok, ok]}]
        r = gates.shots_gates(by, lambda w, h: ("aa", "k"))
        self.assertEqual(r["tears"]["status"], "FAIL")
        self.assertEqual(r["pixels"]["status"], "PASS")
        self.assertEqual(gates.shots_gates(by, lambda w, h: ("bb", "k"))["pixels"]["status"], "FAIL")
        self.assertEqual(gates.shots_gates(by, lambda w, h: (None, "k"))["pixels"]["status"], "SKIP")
        by[1]["shots"][1] = dict(ok, hash="cc")
        self.assertEqual(gates.shots_gates(by, None)["pixels"]["status"], "FAIL")      # the same screen, two pictures
        by2 = [{"idx": 0, "role": "busy", "shots": [dict(ok, tear_applicable=False)]}]
        self.assertEqual(gates.shots_gates(by2, None)["tears"]["status"], "SKIP")

    def test_leak_gate(self):
        self.assertEqual(gates.leak_gate("x", [1, 2, 3, 4], [0, 50, 100, 150], 20, "MB")["status"], "FAIL")
        self.assertEqual(gates.leak_gate("x", [1, 2, 3, 4], [0, 1, 2, 3], 20, "MB")["status"], "PASS")
        self.assertEqual(gates.leak_gate("x", [1], [0], 20, "MB")["status"], "SKIP")


class Schema(unittest.TestCase):
    def test_fake_result_validates_and_is_private(self):
        doc = helpers.fake_scale_result()
        self.assertEqual(schema.validate(doc), [])

    def test_privacy_catches_paths_ips_hosts(self):
        doc = helpers.fake_scale_result()
        doc["env"]["x"] = "/var/lib/something/live.conf"
        self.assertTrue(any("path" in p for p in schema.validate(doc)))
        doc["env"]["x"] = "connected to 192.168.1.20"
        self.assertTrue(any("ip" in p for p in schema.validate(doc)))
        import socket
        doc["env"]["x"] = "ran on %s" % socket.gethostname()
        self.assertTrue(any("host" in p for p in schema.validate(doc)))

    def test_scrub_then_publishable(self):
        import socket
        doc = helpers.fake_scale_result()
        doc["env"]["x"] = "ssh %s from %s at 10.0.0.1" % (socket.gethostname(), os.path.expanduser("~") + "/work")
        clean = schema.scrubber(["my-vm-alias"])
        doc["env"]["y"] = "my-vm-alias"
        d = tempfile.mkdtemp()
        p = os.path.join(d, "r.json")
        problems = schema.write(doc, p, clean)
        self.assertEqual(problems, [])
        back = schema.load(p)
        self.assertTrue(back["publishable"])
        self.assertNotIn(socket.gethostname(), json.dumps(back))
        self.assertEqual(back["env"]["y"], "<host>")

    def test_write_keeps_data_even_when_not_publishable(self):
        doc = helpers.fake_scale_result()
        doc["env"]["x"] = "/var/private/path"
        p = os.path.join(tempfile.mkdtemp(), "r.json")
        problems = schema.write(doc, p)
        self.assertTrue(problems)
        back = schema.load(p)
        self.assertFalse(back["publishable"])
        self.assertEqual(len(back["data"]["steps"]), 3)

    def test_validate_rejects_bad_shapes(self):
        self.assertTrue(schema.validate({"schema": 99}))
        doc = helpers.fake_scale_result()
        del doc["data"]["steps"][0]["system"]
        self.assertTrue(any("system" in p for p in schema.validate(doc)))


class CompareAndChart(unittest.TestCase):
    def test_compare_flags_regressions_beyond_noise(self):
        a = helpers.fake_scale_result(fps=(60, 58, 56))
        b = helpers.fake_scale_result(fps=(60, 58, 40))
        text, worse = compare.compare(a, b)
        self.assertGreater(worse, 0)
        self.assertIn("WORSE", text)
        same, w0 = compare.compare(a, helpers.fake_scale_result(fps=(60, 58, 56)))
        self.assertEqual(w0, 0, same)
        c = helpers.fake_scale_result(fps=(61, 58.5, 56.5))        # +1.7%, +0.9%: inside the 4% noise
        self.assertEqual(compare.compare(a, c)[1], 0)

    def test_compare_max_n(self):
        a = helpers.fake_scale_result(fps=(60, 58, 56))
        b = helpers.fake_scale_result(fps=(60, 58, 40))
        text, _ = compare.compare(a, b)
        self.assertIn("max sustainable N  smooth60", text)

    def test_chart_is_self_contained(self):
        html = chart.scale_chart(helpers.fake_scale_result(ns=(1, 2, 3, 4), fps=(60, 59, 55, 30)))
        self.assertIn("<svg", html)
        self.assertEqual(html.count("<svg"), 8)
        for bad in ("<script", "http://", "https://", "src=", "href="):
            if bad == "http://":
                self.assertEqual(html.count("http://"), html.count('xmlns="http://www.w3.org/2000/svg"'))
            else:
                self.assertNotIn(bad, html)
        import xml.dom.minidom
        for svg in html.split("<svg")[1:]:
            xml.dom.minidom.parseString("<svg" + svg.split("</svg>")[0] + "</svg>")


class AgentAndMotion(unittest.TestCase):
    def test_motion_metrics_ignore_idle_gaps(self):
        t = [i / 60 for i in range(60)] + [5 + i / 60 for i in range(60)]
        m = analysis.motion_metrics(t)
        self.assertEqual(m["stretches"], 2)
        self.assertAlmostEqual(m["fps"], 60.0, delta=0.5)
        self.assertEqual(m["over2x"], 0)

    def test_agent_metrics(self):
        raw = [(10.0 + i / 60, i, 255) for i in range(300)]
        acts = [{"op": "launch", "t0": 10.5, "t1": 10.9, "rc": 0}, {"op": "screenshot", "t0": 12.0, "t1": 12.4, "rc": 0},
                {"op": "terminate", "t0": 13, "t1": 13.1, "rc": 0}]
        m = analysis.agent_metrics(acts, raw)
        self.assertAlmostEqual(m["launch_to_frame_ms_p50"], 17, delta=17)
        self.assertAlmostEqual(m["screenshot_ms_p50"], 400, delta=1)


if __name__ == "__main__":
    unittest.main()
