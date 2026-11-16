"""Pure analysis of one measurement window: raw per-frame lines and probes in, per-simulator metrics out."""
from . import stats, strip, targets

LUMA_MID = 127


def parse_raw(text):
    """simfps's SIMFPS_STAMPS file: '<unix s> <counter or -1> <probe luma or -1>' per line -> [(t, counter, luma)]."""
    out = []
    for line in text.splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                out.append((float(p[0]), int(p[1]), int(p[2])))
            except ValueError:
                pass
    return out


def probe_latencies(raw, injections, timeout_s=2.0):
    """Input-latency PROXY for one simulator.

    raw: [(t, counter, luma)] ascending (the first line is the screen at the start); injections: [(t0, t_return)]
    guest wall-clock seconds, t0 taken just before the injecting command was started. A probe is answered by the
    first surface after t0 whose marker (luma above/below 127) differs from the last one before t0. No such
    surface within timeout_s is a MISS; it counts at the timeout in the percentiles (never dropped).
    What it includes: starting `simctl` (its own duration is `call_ms`), CoreSimulator, the app's handling and
    one frame's composite. What it leaves out: the Device panel stream, the viewer, a real finger's HID path.
    Returns {"n", "misses", "p50_ms", "p95_ms", "max_ms", "call_p50_ms", "latencies_ms": [...]}"""
    lat, calls, misses = [], [], 0
    for t0, tret in injections:
        before = None
        for t, _, luma in raw:
            if t >= t0:
                break
            if luma >= 0:
                before = luma > LUMA_MID
        hit = None
        if before is not None:
            for t, _, luma in raw:
                if t < t0 or luma < 0:
                    continue
                if t > t0 + timeout_s:
                    break
                if (luma > LUMA_MID) != before:
                    hit = t - t0
                    break
        calls.append((tret - t0) * 1000.0)
        if hit is None:
            misses += 1
            lat.append(timeout_s * 1000.0)
        else:
            lat.append(hit * 1000.0)
    return {"n": len(injections), "misses": misses, "p50_ms": stats.r1(stats.percentile(lat, 0.5)),
            "p95_ms": stats.r1(stats.percentile(lat, 0.95)), "max_ms": stats.r1(max(lat)) if lat else None,
            "call_p50_ms": stats.r1(stats.percentile(calls, 0.5)), "latencies_ms": [round(x, 1) for x in lat]}


def sim_metrics(idx, role, raw, injections, nominal_fps=60.0, timeout_s=2.0, app_ticks=None, window=None):
    """One simulator's entry in a step. A busy simulator gets frame metrics from its strip counters (distinct
    frames), every simulator with probes gets the input proxy. `window` = (t_start, t_end) clips the raw lines."""
    if window:
        raw = [r for r in raw if window[0] <= r[0] <= window[1]] or raw[:1]
    sim = {"idx": idx, "role": role}
    if role == "busy":
        a = strip.analyze_counters([(t, c) for t, c, _ in raw])
        fm = stats.frame_stats([t for t, _ in a["unique"]], nominal_fps)
        sim.update(fm)
        sim["strip"] = {k: a[k] for k in ("repeats", "lost", "backwards", "jumps", "unreadable", "span_ticks")}
        ok, why = strip.strip_gate(a, app_ticks)
        sim["strip_gate"] = {"ok": ok, "why": why}
        sim["app_ticks"] = app_ticks
    if injections:
        pl = probe_latencies(raw, injections, timeout_s)
        sim.update({"input_n": pl["n"], "input_misses": pl["misses"], "input_p50_ms": pl["p50_ms"],
                    "input_p95_ms": pl["p95_ms"], "input_max_ms": pl["max_ms"], "input_call_p50_ms": pl["call_p50_ms"]})
    return sim


def fairness(sims):
    """Jain's index and min/max over the distinct fps of the simulators that were meant to animate."""
    f = [s["fps"] for s in sims if s.get("role") == "busy" and s.get("fps") is not None]
    return {"jain": stats.r1(stats.jain(f), 3), "min_max": stats.r1(stats.min_max_ratio(f), 3), "n": len(f)}


STANDING = ("qemu_cpu_cores", "gpu_busy_pct", "guest_cpu_total_pct", "guest_mem_used_mb", "guest_compressed_mb",
            "host_mem_used_mb", "gpu_ms_per_frame", "sims_cpu_pct", "sims_rss_mb")


def flat_system(step):
    """The step's system numbers as one flat {name: value} (nested dicts joined with no prefix clash)."""
    out = {}
    s = step.get("system", {})
    for k, v in s.items():
        if isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, (int, float)):
                    out[k + "_" + k2 if not k2.startswith(k) else k2] = v2
    return out


def standing_cost(steps, names=None):
    """Per-simulator standing cost: the least-squares slope of each system number over N (units per simulator),
    its intercept (the cost with no simulator) and R^2. Steps from `idle` give the pure standing cost."""
    xs = [s["n"] for s in steps]
    flats = [flat_system(s) for s in steps]
    keys = names or sorted({k for f in flats for k in f})
    out = {}
    for k in keys:
        b, a, r2 = stats.slope(xs, [f.get(k) for f in flats])
        if b is not None:
            out[k] = {"per_sim": round(b, 3), "at_zero": round(a, 3), "r2": round(r2, 3)}
    return out


def summarize_scale(steps, target_specs, stopped=None):
    tg = [targets.parse_target(t) for t in target_specs]
    mx = {}
    for t in tg:
        mx[t["name"]] = targets.max_sustainable(steps, t)
    for st in steps:
        st["targets"] = {t["name"]: targets.evaluate(t, st["sims"])["pass"] for t in tg}
        st["fairness"] = fairness(st["sims"])
    return {"max_n": mx, "stopped": stopped, "standing_cost": standing_cost(steps),
            "fairness_min_jain": min([s["fairness"]["jain"] for s in steps if s["fairness"]["jain"] is not None] or [None])
            if any(s["fairness"]["jain"] is not None for s in steps) else None}


def pace_summary(records, t_first, t_last, t_start, t_end):
    """GlassBench's own tick log ([(at, counter, fps, ticks)] every ~5 s, from before the window to after it):
    app_tick_fps is the mean of the records written inside the window (what the app produced, before the compositor);
    app_ticks brackets the frames seen: the counter's distance from the last record at or before the first frame to
    the first record at or after the last one (an upper bound for the ticks between the frames: about 5 s of slack
    on each side). Either is None when the log does not reach."""
    inside = [r[2] for r in records if t_start <= r[0] <= t_end + 5 and r[2] > 0]
    before = [r for r in records if r[0] <= t_first]
    after = [r for r in records if r[0] >= t_last]
    ticks = (after[0][1] - before[-1][1]) % strip.WRAP if before and after else None
    return {"app_tick_fps": stats.r1(stats.mean(inside)), "app_ticks": ticks}


def agent_metrics(actions, raw):
    """The agent mix's actions: launch call and launch-to-first-frame, screenshot duration (ms, p50/p95)."""
    out = {}
    launches = [a for a in actions if a["op"] == "launch" and a.get("rc") == 0]
    shots = [a for a in actions if a["op"] == "screenshot" and a.get("rc") == 0]
    ttff = []
    for a in launches:
        nxt = next((r[0] for r in raw if r[0] >= a["t0"] and r[1] >= 0), None)
        if nxt is not None:
            ttff.append((nxt - a["t0"]) * 1000.0)
    for name, vals in (("launch_to_frame_ms", ttff), ("screenshot_ms", [(a["t1"] - a["t0"]) * 1000.0 for a in shots]),
                       ("launch_call_ms", [(a["t1"] - a["t0"]) * 1000.0 for a in launches])):
        out[name + "_p50"] = stats.r1(stats.percentile(vals, 0.5))
        out[name + "_p95"] = stats.r1(stats.percentile(vals, 0.95))
    out["actions"] = len(actions)
    out["action_failures"] = sum(1 for a in actions if a.get("rc") not in (0, None))
    return out


def motion_metrics(times, nominal_fps=60.0, still_ms=250.0):
    """frame metrics over the stretches of motion only (the agent mix idles between actions): fps = frames per
    second inside stretches; the intervals are the ones inside stretches; the gap that ends a stretch is not one."""
    st = stats.stretches(times, still_ms)
    ivs, frames, span = [], 0, 0.0
    for t0, t1, n in st:
        span += t1 - t0
        frames += n - 1
    inside = [t for t in times]
    for a, b in zip(inside, inside[1:]):
        if (b - a) * 1000.0 <= still_ms:
            ivs.append((b - a) * 1000.0)
    thr = 2000.0 / nominal_fps
    return {"stretches": len(st), "fps": stats.r1(frames / span if span > 0 else 0.0), "p50_ms": stats.r1(stats.percentile(ivs, 0.5)),
            "p95_ms": stats.r1(stats.percentile(ivs, 0.95)), "p99_ms": stats.r1(stats.percentile(ivs, 0.99)),
            "max_ms": stats.r1(max(ivs) if ivs else None), "over2x": sum(1 for x in ivs if x > thr),
            "longest_gap_s": stats.r1(max(ivs) / 1000.0, 3) if ivs else None}
