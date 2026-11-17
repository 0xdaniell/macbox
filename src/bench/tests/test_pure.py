import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from macbench import abrule, analysis, gates, png, stats, strip, targets, tears  # noqa: E402
from tests import helpers  # noqa: E402


class CrashReports(unittest.TestCase):
    NEW = ["searchd-2026-11-17-065234.ips", "SpringBoard-2026-11-17-065301.ips", "GlassBench_2026-11-17-065302_Mac.crash",
           "PassbookWidgetsExtension-iPhone-2026-11-17-065303.ips", "com.apple.CoreSimulator.CoreSimulatorService-2026-11-17-070000.ips"]

    def test_process_name_of_a_report(self):
        self.assertEqual(gates.crash_proc("searchd-2026-11-17-065234.ips"), "searchd")
        self.assertEqual(gates.crash_proc("PassbookWidgetsExtension-iPhone-2026-11-17-065303.ips"), "PassbookWidgetsExtension-iPhone")
        self.assertEqual(gates.crash_proc("GlassBench_2026-11-17-065302_Mac.crash"), "GlassBench")
        self.assertEqual(gates.crash_proc("odd.ips"), "odd")

    def test_only_the_bench_and_the_graphics_path_are_judged(self):
        judged, listed = gates.crash_split(self.NEW)
        self.assertEqual([gates.crash_proc(r) for r in judged], ["GlassBench", "SpringBoard", "com.apple.CoreSimulator.CoreSimulatorService"])
        self.assertEqual([gates.crash_proc(r) for r in listed], ["PassbookWidgetsExtension-iPhone", "searchd"])

    def test_more_names_and_strict(self):
        judged, listed = gates.crash_split(self.NEW, extra=["SearchD", " "])
        self.assertIn("searchd-2026-11-17-065234.ips", judged)
        judged, listed = gates.crash_split(self.NEW, strict=True)
        self.assertEqual((len(judged), listed), (5, []))

    def test_gate_names_what_it_did_not_judge(self):
        g = gates.crash_gate(["searchd-2026-11-17-065234.ips"])
        self.assertEqual(g["status"], "PASS")
        self.assertIn("not judged: 1 other report(s): searchd-2026-11-17-065234.ips", g["detail"])
        g = gates.crash_gate(self.NEW)
        self.assertEqual(g["status"], "FAIL")
        self.assertIn("3 new crash report(s)", g["detail"])
        self.assertEqual(gates.crash_gate([])["status"], "PASS")


class Stats(unittest.TestCase):
    def test_percentile_matches_simfps_rule(self):
        v = list(range(1, 101))
        self.assertEqual(stats.percentile(v, 0.5), 50)      # sorted[int(99*0.5)] = sorted[49]
        self.assertEqual(stats.percentile(v, 0.95), 95)
        self.assertEqual(stats.percentile(v, 0.99), 99)
        self.assertEqual(stats.percentile(v, 1.0), 100)
        self.assertIsNone(stats.percentile([], 0.5))
        self.assertEqual(stats.percentile([7], 0.99), 7)

    def test_frame_stats(self):
        t = [i / 60 for i in range(121)]                    # 2 s at 60 fps
        m = stats.frame_stats(t)
        self.assertAlmostEqual(m["fps"], 60.0, delta=0.1)
        self.assertEqual(m["over2x"], 0)
        t2 = t[:60] + [x + 0.05 for x in t[60:]]            # one 66 ms gap
        m2 = stats.frame_stats(t2)
        self.assertEqual(m2["over2x"], 1)
        self.assertAlmostEqual(m2["longest_gap_s"], 0.067, places=2)
        self.assertEqual(stats.frame_stats([1.0])["fps"], 0.0)

    def test_stretches(self):
        t = [0, .01, .02, .03, 1.0, 1.01, 1.02]
        self.assertEqual(stats.stretches(t, 250), [(0, .03, 4), (1.0, 1.02, 3)])

    def test_jain_and_minmax(self):
        self.assertAlmostEqual(stats.jain([60, 60, 60]), 1.0)
        self.assertAlmostEqual(stats.jain([60, 0, 0]), 1 / 3)
        self.assertAlmostEqual(stats.jain([60, 30]), 0.9, places=6)
        self.assertEqual(stats.jain([0, 0]), 1.0)
        self.assertAlmostEqual(stats.min_max_ratio([30, 60]), 0.5)
        self.assertIsNone(stats.jain([]))

    def test_slope(self):
        b, a, r2 = stats.slope([1, 2, 3, 4], [10, 20, 30, 40])
        self.assertAlmostEqual(b, 10)
        self.assertAlmostEqual(a, 0)
        self.assertAlmostEqual(r2, 1)
        self.assertEqual(stats.slope([1], [1]), (None, None, None))
        self.assertEqual(stats.slope([2, 2], [1, 2]), (None, None, None))
        b, _, _ = stats.slope([1, 2, 3], [5, None, 9])       # None is skipped
        self.assertAlmostEqual(b, 2)


class Strip(unittest.TestCase):
    def test_decode_every_counter_and_scale(self):
        for width in (402, 1206):
            for c in (0, 1, 2, 255, 21845, 43690, 65535):
                self.assertEqual(strip.read_counter_image(helpers.strip_image(c, width)), c, (width, c))

    def test_no_strip_is_unreadable(self):
        img = png.Image(402, 900, 3, [bytes([128] * 402 * 3)] * 900)
        self.assertEqual(strip.read_counter_image(img), -1)

    def test_png_roundtrip_and_hash(self):
        img = helpers.strip_image(1234, 402)
        back = png.read_png(png.write_png(img))
        self.assertEqual(back.pixel_hash(), img.pixel_hash())
        self.assertEqual(strip.read_counter_image(back), 1234)
        other = helpers.strip_image(1235, 402)
        self.assertNotEqual(other.pixel_hash(), img.pixel_hash())

    def test_analyze_clean_with_repeats_and_wrap(self):
        s = [(0.00, 65534), (0.01, 65534), (0.02, 65535), (0.03, 0), (0.04, 0), (0.05, 1)]
        a = strip.analyze_counters(s)
        self.assertEqual([c for _, c in a["unique"]], [65534, 65535, 0, 1])
        self.assertEqual((a["repeats"], a["lost"], a["backwards"], a["jumps"], a["span_ticks"]), (2, 0, 0, 0, 3))
        self.assertTrue(strip.strip_gate(a, app_ticks=3)[0])

    def test_lost_frames_are_counted_not_failed(self):
        a = strip.analyze_counters([(0, 10), (0.03, 13), (0.05, 14)])
        self.assertEqual(a["lost"], 2)
        self.assertTrue(strip.strip_gate(a)[0])

    def test_out_of_order_fails(self):
        a = strip.analyze_counters([(0, 10), (0.02, 12), (0.04, 11), (0.06, 13)])
        self.assertEqual(a["backwards"], 1)
        ok, why = strip.strip_gate(a)
        self.assertFalse(ok)
        self.assertIn("out of order", why)

    def test_jump_and_garbage_fail(self):
        a = strip.analyze_counters([(0, 10), (1, 5000)])
        self.assertEqual(a["jumps"], 1)
        self.assertFalse(strip.strip_gate(a)[0])
        a = strip.analyze_counters([(0, 10), (0.1, 60)])
        self.assertFalse(strip.strip_gate(a, app_ticks=10)[0])       # counter moved 50, the app says 10 ticks
        self.assertTrue(strip.strip_gate(a, app_ticks=50)[0])

    def test_unreadable_share(self):
        s = [(i / 60, i) for i in range(50)] + [(1.0, -1)] * 10
        self.assertFalse(strip.strip_gate(strip.analyze_counters(s))[0])


class Tears(unittest.TestCase):
    def test_clean_screenshot_has_no_tear(self):
        r = tears.detect_tears(helpers.card_image(603))
        self.assertTrue(r["applicable"])
        self.assertFalse(r["tear"], r)

    def test_torn_screenshot_is_found(self):
        r = tears.detect_tears(helpers.card_image(603, tear_at=700))
        self.assertTrue(r["tear"], r)

    def test_no_cards_is_not_applicable(self):
        r = tears.detect_tears(helpers.card_image(603, with_cards=False))
        self.assertFalse(r["applicable"])


class Latency(unittest.TestCase):
    def raw(self, flips, rate=60, secs=10):
        """frames at `rate`; the marker (luma) flips at each time in `flips`"""
        out, lum = [], 255
        for i in range(int(secs * rate)):
            t = 100.0 + i / rate
            if flips and t >= flips[0]:
                lum = 0 if lum == 255 else 255
                flips = flips[1:]
            out.append((t, i, lum))
        return out

    def test_latency_and_misses(self):
        raw = self.raw([102.080, 105.250])                 # answers 80 ms and 250 ms after the injections
        inj = [(102.0, 102.12), (105.0, 105.14), (108.0, 108.1)]     # the third is never answered
        r = analysis.probe_latencies(raw, inj, timeout_s=2.0)
        self.assertEqual(r["misses"], 1)
        self.assertAlmostEqual(r["latencies_ms"][0], 83, delta=17)
        self.assertAlmostEqual(r["latencies_ms"][1], 250, delta=17)
        self.assertEqual(r["latencies_ms"][2], 2000.0)      # a miss counts at the timeout
        self.assertEqual(r["max_ms"], 2000.0)
        self.assertAlmostEqual(r["call_p50_ms"], 120, delta=1)

    def test_static_screen_uses_the_start_line(self):
        raw = [(100.0, -1, 255), (103.07, -1, 0)]          # no frames between: a static screen
        r = analysis.probe_latencies(raw, [(103.0, 103.1)])
        self.assertEqual(r["misses"], 0)
        self.assertAlmostEqual(r["latencies_ms"][0], 70, delta=1)

    def test_sim_metrics_busy_has_frames_and_probe(self):
        raw = [(100 + i / 60, i % 65536, 255) for i in range(300)]
        m = analysis.sim_metrics(0, "busy", raw, [], app_ticks=300)
        self.assertAlmostEqual(m["fps"], 60, delta=0.5)
        self.assertTrue(m["strip_gate"]["ok"])
        s = analysis.sim_metrics(1, "static", raw, [(101.0, 101.1)])
        self.assertNotIn("fps", s)
        self.assertEqual(s["input_misses"], 1)


class Targets(unittest.TestCase):
    def sims(self, fps, p95=20.0, n=3):
        return [{"idx": i, "role": "busy", "fps": f, "p95_ms": p95} for i, f in enumerate(fps)]

    def test_parse(self):
        t = targets.parse_target("smooth: fps>=55, p95<=25")
        self.assertEqual(t["conds"], [("fps", ">=", 55.0), ("p95", "<=", 25.0)])
        with self.assertRaises(ValueError):
            targets.parse_target("x: bogus>=1")
        with self.assertRaises(ValueError):
            targets.parse_target("no colon")

    def test_every_sim_must_pass(self):
        t = targets.parse_target("t: fps>=55")
        self.assertTrue(targets.evaluate(t, self.sims([60, 58, 56]))["pass"])
        r = targets.evaluate(t, self.sims([60, 58, 40]))
        self.assertFalse(r["pass"])
        self.assertEqual(r["failing"][0]["sim"], 2)

    def test_unknown_when_nothing_to_judge(self):
        t = targets.parse_target("t: input_p95<=150")
        self.assertIsNone(targets.evaluate(t, self.sims([60]))["pass"])
        t = targets.parse_target("t: fps>=55, input_p95<=150")        # one condition has no value anywhere: unknown, not a pass
        self.assertIsNone(targets.evaluate(t, self.sims([60]))["pass"])
        self.assertFalse(targets.evaluate(t, self.sims([40]))["pass"])    # but a failed condition still fails it

    def test_max_sustainable_stops_at_first_fail(self):
        steps = [{"n": n, "sims": self.sims([f] * n), "gates_ok": True} for n, f in ((1, 60), (2, 58), (3, 50), (4, 60))]
        t = targets.parse_target("t: fps>=55")
        r = targets.max_sustainable(steps, t)
        self.assertEqual((r["max_n"], r["first_fail"]), (2, 3))      # 4 passes but 3 failed: not sustainable
        self.assertTrue(r["evaluated"][4])

    def test_gates_and_instability_veto(self):
        steps = [{"n": 1, "sims": self.sims([60]), "gates_ok": True},
                 {"n": 2, "sims": self.sims([60, 60]), "gates_ok": False}]
        self.assertEqual(targets.max_sustainable(steps, targets.parse_target("t: fps>=55"))["max_n"], 1)
        steps = [{"n": 1, "sims": self.sims([60]), "stable": False}]
        self.assertIsNone(targets.max_sustainable(steps, targets.parse_target("t: fps>=55"))["max_n"])

    def test_default_targets_parse(self):
        for spec in targets.DEFAULT_TARGETS:
            targets.parse_target(spec)

    def test_summary_and_fairness(self):
        doc = helpers.fake_scale_result()
        sm = doc["data"]["summary"]
        self.assertEqual(sm["max_n"]["smooth60"]["max_n"], 2)
        self.assertEqual(sm["max_n"]["smooth30"]["max_n"], 3)
        self.assertAlmostEqual(doc["data"]["steps"][0]["fairness"]["jain"], 1.0)
        sc = sm["standing_cost"]
        self.assertAlmostEqual(sc["qemu_cpu_cores"]["per_sim"], 1.0, places=2)
        self.assertAlmostEqual(sc["guest_mem_used_mb"]["per_sim"], 900.0, places=1)


class ABRule(unittest.TestCase):
    def test_adopt_clear_win(self):
        r = abrule.decide(100, 110, 100, 111)               # +10, +11
        self.assertEqual(r["decision"], "ADOPT")

    def test_close_below_bar(self):
        self.assertEqual(abrule.decide(100, 102, 100, 103)["decision"], "CLOSE")

    def test_close_when_pairs_disagree(self):
        r = abrule.decide(100, 112, 100, 98)
        self.assertEqual(r["decision"], "CLOSE")
        self.assertTrue(any("disagree" in w for w in r["why"]))

    def test_lower_is_better(self):
        self.assertEqual(abrule.decide(10, 8, 10, 8.5, higher_better=False)["decision"], "ADOPT")
        self.assertEqual(abrule.decide(10, 12, 10, 12, higher_better=False)["decision"], "CLOSE")

    def test_tail_worsening_closes(self):
        r = abrule.decide(100, 110, 100, 110, tails={"p95": (20, 25, 20, 26, False)})
        self.assertEqual(r["decision"], "CLOSE")
        r = abrule.decide(100, 110, 100, 110, tails={"p95": (20, 20.2, 20, 20.3, False)})
        self.assertEqual(r["decision"], "ADOPT")

    def test_missing_values_close(self):
        self.assertEqual(abrule.decide(0, 1, 100, 110)["decision"], "CLOSE")

    def test_decision_line(self):
        r = abrule.decide(100, 110, 100, 111)
        line = abrule.decision_line("knob=1", "distinct fps", ((100, 100), (110, 111)), r, 0.04)
        self.assertIn("ADOPT", line)
        self.assertIn("bar 4.0%", line)


if __name__ == "__main__":
    unittest.main()
