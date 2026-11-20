"""`ab`: one knob, arms interleaved A B B A inside one window, only the within-window difference judged."""
import time

from . import abrule, core, gates, scale, stats
from .core import Stop
from .runctx import CmdKnob, LiveKnob, shell_runner

METRICS = {
    "total_fps": (lambda st: sum(s["fps"] for s in st["sims"] if s.get("fps") is not None), True),
    "min_fps": (lambda st: min([s["fps"] for s in st["sims"] if s.get("fps") is not None] or [None]), True),
    "worst_p95": (lambda st: max([s["p95_ms"] for s in st["sims"] if s.get("p95_ms") is not None] or [None]), False),
    "worst_p99": (lambda st: max([s["p99_ms"] for s in st["sims"] if s.get("p99_ms") is not None] or [None]), False),
    "input_p95": (lambda st: max([s["input_p95_ms"] for s in st["sims"] if s.get("input_p95_ms") is not None] or [None]), False),
    "gpu_ms_per_frame": (lambda st: st["system"].get("gpu_ms_per_frame"), False),
    "qemu_cores": (lambda st: st["system"].get("qemu_cpu_cores"), False),
}
DEFAULT_TAILS = {"total_fps": ["worst_p95", "worst_p99"], "min_fps": ["worst_p95", "worst_p99"], "gpu_ms_per_frame": ["worst_p95"],
                 "qemu_cores": ["min_fps"], "worst_p95": ["min_fps"], "worst_p99": ["min_fps"], "input_p95": ["min_fps"]}


def make_knob(a, ctx):
    if a.knob and a.knob.startswith("live:"):
        k, v = a.knob[5:].split("=", 1)
        if not ctx.live_conf:
            raise Stop("--knob live:KEY=VALUE needs the VM's live switch file (not found)")
        return LiveKnob(ctx.live_conf, k, v, ctx.run)
    if a.set_cmd and a.unset_cmd:
        return CmdKnob(a.set_cmd, a.unset_cmd, ctx.run, shell_runner())
    raise Stop("a knob is needed: --knob live:KEY=VALUE, or --set-cmd and --unset-cmd")


def run_ab(ctx):
    a = ctx.a
    metric = a.metric
    hb = METRICS[metric][1]
    tails = [t for t in (a.tails.split(",") if a.tails else DEFAULT_TAILS.get(metric, [])) if t]
    rule = "AB rule (fixed before the run): knob %s; metric %s (%s better); ADOPT only if the mean effect of the 2 pairs exceeds %.1f%% and both pairs agree in sign; tails %s may not worsen by more than %.1f%%; order A B B A; no re-run" % (
        a.knob or "cmd", metric, "higher" if hb else "lower", a.threshold * 100, ",".join(tails) or "none", a.tail_tol * 100)
    ctx.log(rule)
    ctx.doc["params"]["rule"] = rule
    needs_input = any(m.startswith("input_") for m in [metric] + tails)          # the metric or a tail it must not worsen
    if needs_input and a.mix not in ("all-busy", "one-busy"):
        raise Stop("%s needs input probes, which run only in the all-busy and one-busy mixes" % ",".join(m for m in [metric] + tails if m.startswith("input_")))
    knob = make_knob(a, ctx)
    info = core.setup_guest(ctx)
    core.capture_env(ctx, info)
    ctx.marks = {"coh": ctx.coh.mark(), "t0": time.time()}
    n, mix = a.n, a.mix
    reason = core.guard(ctx, n, "before the A/B")
    if reason:
        raise Stop(reason)
    ctx.host.n = n
    core.grow(ctx, n, core.roles_for(mix, n))
    ctx.probe_method = scale.calibrate(ctx, ctx.sims[0])["chosen"] if needs_input else "none"
    core.settle(ctx, "before the A/B")
    ctx.doc["data"] = {"knob": knob.describe(), "metric": metric, "arms": [], "n": n, "mix": mix}
    vals = {"A": [], "B": []}
    steps = {"A": [], "B": []}
    bad = None
    ctx.run.on_deadline = lambda reason=None: (_save(ctx), None)[1]
    for arm in abrule.abba_order():
        (knob.set if arm == "B" else knob.unset)()
        time.sleep(a.arm_settle)
        g, s0, s1, t_go = core.window(ctx, a.secs, mix)
        step = core.build_step(ctx, n, mix, g, s0, s1, t_go)
        core.step_gates(ctx, step)
        steps[arm].append(step)
        ctx.doc["data"].setdefault("steps", []).append(dict(step, arm=arm))          # the whole step: per simulator, gates
        v = {k: f(step) for k, (f, _) in METRICS.items()}
        vals[arm].append(v)
        ctx.doc["data"]["arms"].append({"arm": arm, "values": v, "gates_ok": step["gates_ok"], "stable": step["stable"]})
        _save(ctx)
        if not (step["gates_ok"] and step["stable"]) and bad is None:
            bad = "arm %s failed its gates or stability: %s" % (arm, [k for k, g in step["gates"].items() if g["status"] == "FAIL"] or step["stability"])
    knob.unset()
    a1, a2 = vals["A"][0][metric], vals["A"][1][metric]
    b1, b2 = vals["B"][0][metric], vals["B"][1][metric]
    tl = {t: (vals["A"][0][t], vals["B"][0][t], vals["A"][1][t], vals["B"][1][t], METRICS[t][1]) for t in tails}
    res = abrule.decide(a1, b1, a2, b2, hb, a.threshold, tl, a.tail_tol)
    if bad:
        res["decision"] = "CLOSE"
        res["why"].append(bad)
    line = abrule.decision_line(knob.describe() if a.knob else "cmd", metric, ((a1, a2), (b1, b2)), res, a.threshold)
    ctx.doc["data"]["decision"] = res
    ctx.doc["data"]["line"] = line
    ctx.doc["data"]["pairs"] = [{"pair": 1, "A": a1, "B": b1}, {"pair": 2, "A": a2, "B": b2}]
    ctx.section("ab", "PASS" if not bad else "FAIL", line)
    ctx.save()


def _save(ctx):
    ctx.save()
