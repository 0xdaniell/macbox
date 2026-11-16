"""The scaling sweep: N = 1, 2, 3, ... simulators, per-simulator numbers at each N, the maximum sustainable N per
quality target."""
import time

from . import analysis, core, gates, stats, targets
from .core import Stop
from .runctx import DeadlineError
from .transport import GuestError

STEP_GATES = ("strip", "kernel_gpu", "coherent_census", "coherent_process", "qemu_log", "gpu_hang", "tears", "pixels", "memory")


def calibrate(ctx, sim):
    """Pick the input injection that is answered (or 'none'): 3 tries of each method on one simulator."""
    a = ctx.a
    if a.probe_method != "auto":
        ctx.probe_method = a.probe_method
        return {"chosen": a.probe_method, "calibrated": False}
    methods = "openurl,appearance"
    r = ctx.guest.call(["calibrate", "--udid", sim["udid"], "--methods", methods, "--time-scale", 0.3 if ctx.host.dry else 1.0] + (["--probe-cmd", a.probe_cmd] if a.probe_cmd else []), 200)
    chosen = next((m for m in methods.split(",") if r["methods"].get(m, {}).get("answered", 0) >= 2), "none")
    ctx.probe_method = chosen
    return {"chosen": chosen, "calibrated": True, "methods": r["methods"]}


def golden_phase(ctx, record=False):
    """Pixel-exact reference: the first simulator shows GlassBench -static; its screenshots must agree and match
    the stored golden. Returns the pixels gate. Costs a few seconds."""
    s0 = ctx.sims[0]
    keep = s0["role"]
    ctx.guest.call(["prepare", "--udids", s0["udid"], "--roles", "static"] + (["--quiesce"] if ctx.a.quiesce else []), 120)
    s0["role"] = "static"
    time.sleep(0.3 if ctx.host.dry else 3)
    r = ctx.guest.call(["shots", "--udids", s0["udid"], "--count", 3], 120)
    by = [{"idx": 0, "role": "static", "shots": r["sims"][0]["shots"]}]
    g = gates.shots_gates(by, ctx.golden)["pixels"]
    ok = [x for x in by[0]["shots"] if x.get("ok")]
    ctx.golden_seen = {"hash": ok[0]["hash"], "w": ok[0]["w"], "h": ok[0]["h"]} if ok and len({x["hash"] for x in ok}) == 1 else None
    s0["role"] = None            # forces grow() to launch it with its real role
    return g


def run_scale(ctx):
    a = ctx.a
    doc, mix = ctx.doc, a.mix
    tg_specs = a.targets
    floor = targets.parse_target("floor: " + a.floor)
    info = core.setup_guest(ctx)
    core.capture_env(ctx, info)
    data = doc["data"] = {"mix": mix, "scale_x": a.scale_x, "device": doc["env"]["guest"].get("device_type"), "ns": a.ns,
                          "nominal_fps": a.nominal_fps, "window_s": a.secs, "targets": tg_specs, "floor": a.floor, "steps": [],
                          "summary": {}, "calibration": None, "observer": None}
    ctx.marks = {"coh": ctx.coh.mark(), "t0": time.time()}
    ctx.run.on_deadline = lambda: (_finish(ctx, "deadline"), None)[1]
    stopped, first, kind = None, True, None
    try:
        for n in a.ns:
            roles = core.roles_for(mix, n)
            reason = core.guard(ctx, n - len(ctx.sims), "before booting simulator %d" % n)
            if reason:
                stopped, kind = reason, "guard"
                break
            ctx.host.n = n
            boot = core.grow(ctx, n, roles)
            if first:
                if mix in ("all-busy", "one-busy"):
                    data["calibration"] = calibrate(ctx, ctx.sims[0])
                    ctx.guest.call(["settle", "--max-s", 20, "--bg-pct", 90, "--step", ctx.a.settle_step], 60)
                if a.golden_phase:
                    ctx.pixel_gate = golden_phase(ctx)
                    core.grow(ctx, n, roles)
            core.settle(ctx)
            g, s0, s1, t_go = core.window(ctx, a.secs, mix)
            step = core.build_step(ctx, n, mix, g, s0, s1, t_go, boot)
            shots, by = core.take_shots(ctx, a.shots) if a.shots and mix != "idle" else ({}, [])
            if first and ctx.pixel_gate and "pixels" not in shots:
                shots["pixels"] = ctx.pixel_gate
            core.step_gates(ctx, step, shots)
            step["window"] = {"start_offset_s": round(t_go - ctx.marks["t0"] - ctx.offset, 1)}
            data["steps"].append(step)
            if first and a.observer_check and mix in ("all-busy", "one-busy", "agent"):
                data["observer"] = observer_check(ctx, step, mix)
            _est(ctx, data["steps"])
            _finish(ctx, None, partial=True)
            first = False
            if not step["gates_ok"]:
                bad = [k for k, v in step["gates"].items() if v["status"] == "FAIL"]
                stopped, kind = "gate failed at N=%d: %s" % (n, ", ".join(bad)), "gate"
                break
            if not step["stable"]:
                stopped, kind = "instability at N=%d: %s" % (n, _why_unstable(step)), "instability"
                break
            fl = targets.evaluate(floor, step["sims"])
            if fl["pass"] is False:
                kind = "floor"
                stopped = "floor %s breached at N=%d (sim %s: %s = %s)" % (a.floor, n, fl["failing"][0]["sim"], fl["failing"][0]["cond"], fl["failing"][0]["value"])
                break
    except Stop as e:
        stopped, kind = str(e), "error"
    except (DeadlineError, GuestError) as e:
        stopped, kind = "%s: %s" % (type(e).__name__, e), "error"
    data["stopped_kind"] = kind
    return _finish(ctx, stopped)


def _why_unstable(step):
    s = step["stability"]
    out = []
    if s["new_ips"]:
        out.append("%d new crash report(s)" % s["new_ips"])
    if s["sims_died"]:
        out.append("simulator(s) %s no longer booted" % s["sims_died"])
    if s["kernel_gpu_lines"]:
        out.append("%d kernel GPU error line(s)" % s["kernel_gpu_lines"])
    if not s["qemu_same"]:
        out.append("QEMU restarted")
    return "; ".join(out)


def _est(ctx, steps):
    """A simulator's guest memory from the steps so far (for the guard)."""
    if len(steps) >= 2:
        d = [(b["system"]["guest"]["mem_used_mb"] - a["system"]["guest"]["mem_used_mb"]) / max(1, b["n"] - a["n"]) for a, b in zip(steps, steps[1:])]
        ctx.est_sim_mb = max(300.0, max(d))
    elif steps:
        ctx.est_sim_mb = max(ctx.est_sim_mb, 1100.0)


def observer_check(ctx, step, mix):
    """Decision: if reading the strip moves the surface frame rate by more than the noise (4%), every distinct-frame
    number in this result carries that observer cost. A second window of the same N reads no strip."""
    g, s0, s1, t_go = core.window(ctx, ctx.a.secs, mix, no_strip=True)
    with_strip = [s["surface_fps"] for s in step["sims"] if s.get("role") in ("busy", "agent") and s.get("surface_fps")]
    without = [s["simfps"]["fps"] for s in g["sims"] if s["role"] in ("busy", "agent") and s.get("simfps")]
    ws, wo = stats.mean(with_strip), stats.mean(without)
    d = stats.rel_diff(wo, ws)
    return {"n": step["n"], "surface_fps_with_strip": stats.r1(ws), "surface_fps_without_strip": stats.r1(wo),
            "delta": None if d is None else round(d, 4), "ok": d is not None and abs(d) <= 0.04}


def _finish(ctx, stopped, partial=False):
    """Summary, sections and the result file. Also the deadline's partial write (partial=True keeps the doc open)."""
    a, doc = ctx.a, ctx.doc
    data = doc["data"]
    steps = data["steps"]
    if not steps and not stopped:
        return None
    data["summary"] = analysis.summarize_scale(steps, a.targets, stopped) if steps else {"max_n": {}, "stopped": stopped, "standing_cost": {}}
    doc["data"]["summary"]["stopped"] = stopped
    doc["gates"] = _gate_rollup(steps, ctx)
    if not partial:
        doc["sections"] = [s for s in doc["sections"] if s["name"] not in ("scale", "gates", "stability", "observer", "input", "targets", "cost")]
        _sections(ctx, data, stopped)
    ctx.save()
    return data["summary"]


def _gate_rollup(steps, ctx):
    roll = {}
    for name in STEP_GATES:
        rows = [(st["n"], st["gates"].get(name)) for st in steps if st.get("gates", {}).get(name)]
        if not rows:
            roll[name] = gates.gate("SKIP", "not run")
            continue
        bad = [(n, g) for n, g in rows if g["status"] == "FAIL"]
        if bad:
            roll[name] = gates.gate("FAIL", "N=%d: %s" % (bad[0][0], bad[0][1]["detail"]))
        elif all(g["status"] == "SKIP" for _, g in rows):
            roll[name] = gates.gate("SKIP", rows[-1][1]["detail"])
        else:
            roll[name] = gates.gate("PASS", "%d step(s); %s" % (len(rows), rows[-1][1]["detail"]))
    return roll


def _sections(ctx, data, stopped):
    a = ctx.a
    steps = data["steps"]
    sm = data["summary"]
    gate_fail = [k for k, v in ctx.doc["gates"].items() if v["status"] == "FAIL"]
    unstable = any(not s["stable"] for s in steps)
    mx = ", ".join("%s=%s" % (k, v["max_n"] if v["max_n"] is not None else "none") for k, v in sm["max_n"].items())
    status = "FAIL" if gate_fail or unstable or data.get("stopped_kind") == "error" else "PASS"
    ctx.section("scale", status, "SCALE %s %s %dx %s: max sustainable N: %s | steps %s%s | min Jain %s" % (
        status, a.mix, a.scale_x, "(DRY)" if ctx.host.dry else "", mx, [s["n"] for s in steps],
        (" | stopped: %s" % stopped) if stopped else " | all requested N done", sm.get("fairness_min_jain")))
    for name, v in ctx.doc["gates"].items():
        ctx.section("gate:" + name, v["status"] if v["status"] != "PASS" else "PASS", "GATE %s %s: %s" % (v["status"], name, v["detail"]))
    st = [s for s in steps]
    ctx.section("stability", "FAIL" if unstable else "PASS", "STABILITY %s: %s" % (
        "FAIL" if unstable else "PASS", "; ".join("N=%d %s" % (s["n"], _why_unstable(s)) for s in st if not s["stable"]) or "no crash report, no dead simulator, no kernel GPU line, one QEMU"))
    cal = data.get("calibration")
    if cal:
        ctx.section("input", "INFO", "INPUT PROXY method %s %s" % (cal["chosen"], "(calibrated: %s)" % {k: "%d/%d" % (v["answered"], v["n"]) for k, v in cal.get("methods", {}).items()} if cal.get("calibrated") else "(given)"))
    ob = data.get("observer")
    if ob:
        ctx.section("observer", "PASS" if ob["ok"] else "FAIL", "OBSERVER %s: surface fps %s with the strip read, %s without (%s): %s" % (
            "PASS" if ob["ok"] else "FAIL", ob["surface_fps_with_strip"], ob["surface_fps_without_strip"],
            "n/a" if ob["delta"] is None else "%+.1f%%" % (ob["delta"] * 100), "within the noise" if ob["ok"] else "reading the strip changes the system: distinct-fps numbers are lower bounds"))
    sc = sm.get("standing_cost") or {}
    if sc:
        parts = ["%s %+.2f/sim" % (k, v["per_sim"]) for k, v in sc.items() if k in ("qemu_cpu_cores", "gpu_busy_pct", "guest_mem_used_mb", "guest_sims_cpu_pct", "guest_sims_rss_mb")]
        ctx.section("cost", "INFO", "STANDING COST per simulator (slope over N): " + ", ".join(parts))
