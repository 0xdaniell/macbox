"""The other scenarios: idle VM, lifecycle (+ leak per cycle, boot storm), soak, build timing, the Device panel plug,
the golden recorder."""
import json
import os
import subprocess
import time

from . import analysis, core, gates, scale, stats, strip
from .core import Stop


def _fmt(x, unit=""):
    return "n/a" if x is None else "%g%s" % (x, unit)


def run_idle(ctx):
    a = ctx.a
    info = core.setup_guest(ctx, sims=False)
    core.capture_env(ctx, info)
    ctx.marks = {"coh": ctx.coh.mark(), "t0": time.time()}
    m = ctx.guest.call(["memcheck"], 60)
    if (m["booted"] or m.get("foreign_booted")) and not a.allow_foreign:
        raise Stop("simulators are booted (%d of the bench, %d outside it): an idle VM has none; --allow-foreign to measure anyway" % (m["booted"], m.get("foreign_booted", 0)))
    core.settle(ctx)
    g, s0, s1, t_go = core.window(ctx, a.secs, "idle")
    sysd = core.system_dict(ctx, g, s0, s1, 0)
    kern, note = ctx.host.kernel_gpu(s0["wall"])
    gs = dict(ctx.coh.check(ctx.marks["coh"]))
    gs["kernel_gpu"] = gates.kernel_gate(kern, note)
    ctx.doc["data"] = {"window_s": a.secs, "system": sysd}
    ctx.doc["gates"] = gs
    ok = sysd["qemu_cpu_cores"] <= a.bar_qemu_cores
    ctx.section("idle", "PASS" if ok and gates.worst(gs) else "FAIL",
                "IDLE %s: QEMU %.3f cores (bar <= %g; user %.3f guest %.3f sys %.3f), GPU busy %s%%, guest CPU %s%%, guest used %s MB, host cgroup swap %s MB; gates %s" % (
                    "PASS" if ok and gates.worst(gs) else "FAIL", sysd["qemu_cpu_cores"], a.bar_qemu_cores, sysd["qemu_user_cores"], sysd["qemu_guest_cores"],
                    sysd["qemu_sys_cores"], sysd["gpu_busy_pct"], sysd["guest"]["total_cpu_pct"], sysd["guest"]["mem_used_mb"],
                    sysd["host"].get("cgroup_swap_mb"), "ok" if gates.worst(gs) else [k for k, v in gs.items() if v["status"] == "FAIL"]))
    ctx.save()


def _median(v):
    return stats.r1(stats.percentile(v, 0.5), 2)


def run_lifecycle(ctx):
    a = ctx.a
    info = core.setup_guest(ctx)
    core.capture_env(ctx, info)
    ctx.marks = {"coh": ctx.coh.mark(), "t0": time.time()}
    cyc, xs, rss, used, comp = [], [], [], [], []
    base = ctx.host.snap()["mem"].get("qemu_rss_kb")
    mem0 = ctx.guest.call(["memcheck"], 60)["mem"]
    data = ctx.doc["data"] = {"cycles": cyc, "storm": None}
    for i in range(a.cycles):
        r = ctx.guest.call(["cycle", "--type", ctx.type_id, "--runtime", ctx.runtime], 900)
        cyc.append(r["timings"])
        m = ctx.guest.call(["memcheck"], 60)
        xs.append(i + 1)
        used.append(m["mem"]["used_mb"] - mem0["used_mb"])
        comp.append(m["mem"]["compressed_mb"])
        sn = ctx.host.snap()["mem"].get("qemu_rss_kb")
        rss.append(None if sn is None or base is None else (sn - base) / 1024.0)
        ctx.save()
    keys = sorted({k for c in cyc for k in c})
    med = {k: _median([c[k] for c in cyc if c.get(k) is not None]) for k in keys}
    data["median_s"] = med
    ok = all(v is not None for v in med.values()) and bool(cyc)
    ctx.section("lifecycle", "PASS" if ok else "FAIL", "LIFECYCLE %s (%d cycles, medians, s): create %s, cold boot %s, install %s, launch call %s, first app frame %s, shutdown %s, resume boot %s, first frame after resume %s, clone %s, clone boot %s, delete %s" % (
        "PASS" if ok else "FAIL", len(cyc), med.get("create_s"), med.get("cold_boot_s"), med.get("install_s"), med.get("cold_launch_call_s"),
        med.get("cold_first_frame_s"), med.get("shutdown_s"), med.get("resume_boot_s"), med.get("resume_first_frame_s"), med.get("clone_s"), med.get("clone_boot_s"), med.get("delete_s")))
    leaks = {"qemu_rss": gates.leak_gate("QEMU RSS", xs, rss, a.leak_rss_mb, "MB/cycle"),
             "guest_used": gates.leak_gate("guest used memory", xs, used, a.leak_guest_mb, "MB/cycle")}
    data["leak"] = {"qemu_rss_mb": rss, "guest_used_mb": used}
    for k, g in leaks.items():
        ctx.doc["gates"]["leak_" + k] = g
        ctx.section("leak:" + k, g["status"], "LEAK %s: %s" % (g["status"], g["detail"]))
    if a.storm:
        n = a.storm
        reason = core.guard(ctx, n, "before the boot storm of %d" % n)
        if reason:
            ctx.section("storm", "SKIP", "BOOT STORM SKIP: " + reason)
        else:
            t0 = time.time()
            boot = core.grow(ctx, n, ["busy"] * n)
            usable = time.time() - t0
            data["storm"] = {"n": n, "boot_all_s": boot.get("all_usable_s"), "each_s": boot.get("each_usable_s"), "boot_and_launch_s": round(usable, 1)}
            ok = usable <= a.storm_bar_s
            ctx.section("storm", "PASS" if ok else "FAIL", "BOOT STORM %s: %d simulators booted at once: all booted in %s s (slowest %s s), booted and app launched in %.0f s (bar <= %g)" % (
                "PASS" if ok else "FAIL", n, boot.get("all_usable_s"), max(boot.get("each_usable_s") or [0]), usable, a.storm_bar_s))
    ctx.doc["gates"].update(ctx.coh.check(ctx.marks["coh"]))
    ctx.save()


def run_soak(ctx):
    a = ctx.a
    info = core.setup_guest(ctx)
    core.capture_env(ctx, info)
    ctx.marks = {"coh": ctx.coh.mark(), "t0": time.time()}
    n = a.soak_n
    reason = core.guard(ctx, n, "before the soak")
    if reason:
        raise Stop(reason)
    ctx.host.n = n
    core.grow(ctx, n, ["busy"] * n)
    core.settle(ctx)
    res = {}
    for label in ("start", "end"):
        if label == "end":
            t_end = time.time() + a.soak_minutes * 60
            xs, rss, used = [], [], []
            while time.time() < t_end and not ctx.run.expired.is_set():
                ctx.run.need(60, "a soak sample")
                time.sleep(min(a.soak_every, max(0, t_end - time.time())))
                m = ctx.guest.call(["memcheck"], 60)
                sn = ctx.host.snap()["mem"].get("qemu_rss_kb")
                xs.append((time.time() - ctx.marks["t0"]) / 60.0)
                used.append(m["mem"]["used_mb"])
                rss.append(None if sn is None else sn / 1024.0)
            ctx.doc["data"]["soak"] = {"minutes": xs, "qemu_rss_mb": rss, "guest_used_mb": used}
        g, s0, s1, t_go = core.window(ctx, a.secs, "all-busy")
        res[label] = core.build_step(ctx, n, "all-busy", g, s0, s1, t_go)
    ctx.doc["data"]["steps"] = [res["start"], res["end"]]
    for k, gname, ys, bar in (("QEMU RSS", "leak_qemu_rss", rss, a.soak_rss_mb_min), ("guest used memory", "leak_guest_used", used, a.soak_guest_mb_min)):
        gt = gates.leak_gate(k, xs, ys, bar, "MB/min")
        ctx.doc["gates"][gname] = gt
        ctx.section(gname, gt["status"], "SOAK %s: %s" % (gt["status"], gt["detail"]))
    f0 = min([s["fps"] for s in res["start"]["sims"] if s.get("fps")] or [0])
    f1 = min([s["fps"] for s in res["end"]["sims"] if s.get("fps")] or [0])
    ok = f0 > 0 and f1 >= 0.95 * f0
    ctx.section("soak_fps", "PASS" if ok else "FAIL", "SOAK %s: slowest simulator %.1f fps at the start, %.1f after %g min (bar: not below 95%%)" % ("PASS" if ok else "FAIL", f0, f1, a.soak_minutes))
    ctx.doc["gates"].update(ctx.coh.check(ctx.marks["coh"]))
    ctx.doc["gates"]["strip"] = gates.strip_gate_of(res["end"]["sims"])
    ctx.save()


def run_build(ctx):
    a = ctx.a
    info = core.setup_guest(ctx, sims=False)
    core.capture_env(ctx, info)
    r = ctx.guest.call(["build"] + (["--xcodebuild", a.xcodebuild] if a.xcodebuild else []), 3900)
    ctx.doc["data"] = r
    ok = r.get("tools_cold_rc") == 0 and r.get("tools_warm_rc") == 0
    ctx.section("build", "PASS" if ok else "FAIL", "BUILD %s: bench tools (swiftc, GlassBench + simfps + GenieBench) cold %s s, warm %s s" % ("PASS" if ok else "FAIL", r.get("tools_cold_s"), r.get("tools_warm_s")))
    if a.xcodebuild:
        ok = r.get("xcodebuild_cold_rc") == 0 and r.get("xcodebuild_warm_rc") == 0
        ctx.section("xcodebuild", "PASS" if ok else "FAIL", "XCODEBUILD %s: cold %s s, warm %s s" % ("PASS" if ok else "FAIL", r.get("xcodebuild_cold_s"), r.get("xcodebuild_warm_s")))
    else:
        ctx.section("xcodebuild", "SKIP", "XCODEBUILD SKIP: GlassBench is not an Xcode project; pass --xcodebuild '<project args>' to time yours (cold: new DerivedData, warm: again)")
    ctx.save()


def run_panel(ctx):
    """The Device panel's stream, measured outside this tool: --panel-cmd runs a command that prints one JSON object,
    {"frames": [[t_seconds, counter], ...]}: each frame the viewer received and the counter GlassBench's strip showed in it."""
    a = ctx.a
    ctx.doc["env"].update({"build_id": a.build_id or "unknown"})
    cmd = a.panel_cmd or os.environ.get("MACBOX_BENCH_PANEL_CMD")
    if not cmd:
        ctx.section("panel", "SKIP", "PANEL SKIP: no --panel-cmd (needs Simlock and T3 Code's Device panel; see README, 'The panel section')")
        ctx.save()
        return
    p = ctx.run.popen(["/bin/sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                      env=dict(os.environ, BENCH_PANEL_SECS=str(a.secs)))
    try:
        out, err = p.communicate(timeout=a.secs + 300)
    except subprocess.TimeoutExpired:
        ctx.run.kill_children()
        ctx.section("panel", "FAIL", "PANEL FAIL: the command did not finish")
        ctx.save()
        return
    try:
        frames = json.loads(out.strip().splitlines()[-1])["frames"]
    except (ValueError, KeyError, IndexError):
        ctx.section("panel", "FAIL", "PANEL FAIL: no JSON with frames on the command's last line (%s)" % err.strip()[-100:])
        ctx.save()
        return
    an = strip.analyze_counters([(t, c) for t, c in frames])
    m = stats.frame_stats([t for t, _ in an["unique"]], a.nominal_fps)
    ok_s, why = strip.strip_gate(an)
    from . import targets
    tg = targets.parse_target("panel: fps>=%g, p95<=%g" % (a.panel_fps, a.panel_p95))
    ev = targets.evaluate(tg, [m])
    ctx.doc["data"] = {"metrics": m, "strip": {k: an[k] for k in ("repeats", "lost", "backwards", "jumps", "unreadable")}}
    ctx.doc["gates"]["strip"] = gates.gate("PASS" if ok_s else "FAIL", why)
    ok = bool(ev["pass"]) and ok_s
    ctx.section("panel", "PASS" if ok else "FAIL", "PANEL %s: %s distinct/s, interval p50 %s p95 %s p99 %s max %s ms, %d over 2x nominal, longest gap %s s (bars: >= %g/s, p95 <= %g ms); strip %s" % (
        "PASS" if ok else "FAIL", m["fps"], m["p50_ms"], m["p95_ms"], m["p99_ms"], m["max_ms"], m["over2x"], m["longest_gap_s"], a.panel_fps, a.panel_p95, why))
    ctx.save()


def run_golden(ctx):
    """Records the pixel hash of GlassBench -static for this setup (macOS runtime, device type, scale, size)."""
    a = ctx.a
    info = core.setup_guest(ctx)
    core.capture_env(ctx, info)
    core.grow(ctx, 1, ["static"])
    core.settle(ctx)
    scale.golden_phase(ctx)
    seen = getattr(ctx, "golden_seen", None)
    if not seen:
        ctx.section("golden", "FAIL", "GOLDEN FAIL: the static screen gave different pictures, or none: nothing recorded")
    else:
        key = ctx.golden_key_for(seen["w"], seen["h"])
        ctx.record_golden(key, seen["hash"])
        ctx.section("golden", "PASS", "GOLDEN recorded for %s: %s" % (key, seen["hash"][:16]))
    ctx.save()
