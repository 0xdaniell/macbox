"""The parts every scenario shares: the context, the guard, simulator preparation, one measurement window and the
step record built from it."""
import time

from . import analysis, gates, hostprobe, schema, stats, targets


class Stop(Exception):
    """A reason to end the sweep early; the result so far is kept."""


class Ctx:
    def __init__(self, args, run, guest, host, coherent, doc, out_path, clean, log=print):
        self.a, self.run, self.guest, self.host, self.coh = args, run, guest, host, coherent
        self.doc, self.out_path, self.clean, self.log = doc, out_path, clean, log
        self.offset = 0.0
        self.sims = []              # [{"udid", "idx"}] booted and prepared, in order
        self.type_id = self.runtime = None
        self.probe_method = None
        self.cleanup_info = {}
        self.live_conf = None
        self.est_sim_mb = 1100.0    # a simulator's guest memory, until measured
        self.marks = {}
        self.golden = None          # fn(w, h) -> (hash or None, key)
        self.pixel_gate = None

    def save(self):
        return schema.write(self.doc, self.out_path, self.clean)

    def section(self, name, status, line, detail=None):
        schema.add_section(self.doc, name, status, line, detail)
        self.log("%s %s" % (status, line))


def capture_env(ctx, info):
    host, sw, qenv = ctx.host.env()
    g = {k: info.get(k) for k in ("macos", "xcode", "arch", "cpus", "mem_mb")}
    rts = info.get("runtimes") or []
    g["runtimes"] = ["%s %s" % (r.get("name"), r.get("build")) for r in rts]
    ctx.doc["env"].update({"build_id": ctx.a.build_id or "unknown", "host": host, "guest": g,
                           "switches": sw, "qemu_env": qenv, "tags": dict(kv.split("=", 1) for kv in (ctx.a.tag or []) if "=" in kv),
                           "dry": bool(ctx.host.dry)})


def guard(ctx, k_new, label):
    """Refuses to boot k_new more simulators when the guest or the host would run short: returns a reason or None.
    The sweep stops there; it never swaps the VM to death."""
    a = ctx.a
    hm = ctx.host.mem_avail_gb()
    if hm is not None and hm - k_new * 0.5 < a.min_host_free_gib:
        return "host free memory %.1f GiB below the %.1f GiB floor %s" % (hm, a.min_host_free_gib, label)
    df = ctx.host.disk_free_gb(a.disk_path or "/")
    if df is not None and df < a.min_disk_gib:
        return "free disk %.0f GiB below the %.0f GiB floor %s" % (df, a.min_disk_gib, label)
    m = ctx.guest.call(["memcheck"], 60)
    free_gib = (m["mem"]["free_mb"] - k_new * ctx.est_sim_mb) / 1024.0
    if free_gib < a.min_guest_free_gib:
        return "guest free memory would be %.1f GiB (%.1f now, %d x ~%d MB to boot), below the %.1f GiB floor %s" % (
            free_gib, m["mem"]["free_mb"] / 1024.0, k_new, ctx.est_sim_mb, a.min_guest_free_gib, label)
    if (m.get("pressure") or 1) >= 2:
        return "guest memory pressure level %s %s" % (m.get("pressure"), label)
    if m.get("foreign_booted", 0) > 0 and not a.allow_foreign:
        return "%d simulator(s) outside the bench are booted (the pool or someone's lease): not a clean measurement; --allow-foreign to go on" % m["foreign_booted"]
    return None


def roles_for(mix, n):
    if mix == "all-busy":
        return ["busy"] * n
    if mix == "one-busy":
        return ["busy"] + ["static"] * (n - 1)
    if mix == "agent":
        return ["agent"] * n
    if mix == "idle":
        return ["idle"] * n
    raise ValueError("unknown mix %r" % mix)


def setup_guest(ctx):
    """Device set, device type, tools check. Returns the guest info."""
    a = ctx.a
    info = ctx.guest.call(["info"], 120)
    if not info.get("has_simfps") or not info.get("has_app"):
        raise Stop("the bench tools are not built in the guest: macbox bench tools")
    ctx.guest.call(["init-set"], 60)
    ctx.run.teardown.append(("bench simulators", lambda: ctx.cleanup_info.update(ctx.guest.call(["teardown"], 240))))
    t = ctx.guest.call(["ensure-type", "--scale", str(a.scale_x)] + (["--allow-restart"] if a.allow_restart else []), 180)
    if t.get("needs_restart"):
        raise Stop("device type %s was just created and CoreSimulatorService must restart to load it, which would shut "
                   "down every booted simulator: stop them, then run again with --allow-restart (or `macbox pool create`)" % t["type"])
    ctx.type_id, ctx.runtime = t["type"], t["runtime"]
    ctx.doc["env"]["guest"]["runtime_used"] = t.get("runtime_version")
    ctx.doc["env"]["guest"]["device_type"] = "stock %s" % t["stock_name"] if a.scale_x == 3 else "macbox %dx of %s" % (a.scale_x, t["stock_name"])
    ctx.offset = ctx.guest.clock_offset()
    return info


def grow(ctx, n, roles, install=True):
    """Make and boot simulators up to n (the ones already running stay), launch each with its role. Returns boot timings."""
    have = len(ctx.sims)
    new = n - have
    boot = {}
    if new > 0:
        r = ctx.guest.call(["create", "--n", new, "--type", ctx.type_id, "--runtime", ctx.runtime, "--start", have], 240)
        fresh = r["sims"]
        us = ",".join(s["udid"] for s in fresh)
        b = ctx.guest.call(["boot", "--udids", us] + (["--parallel"] if new > 1 else []), 900)
        bad = [s for s in b["sims"] if not s.get("ok")]
        if bad:
            raise Stop("a simulator did not boot: %s" % bad[0].get("err"))
        for s, bs in zip(fresh, b["sims"]):
            ctx.sims.append({"idx": s["idx"], "udid": s["udid"]})
        boot = {"new": new, "all_usable_s": b["all_s"], "each_usable_s": [s["usable_s"] for s in b["sims"]]}
    # roles may change with N (one-busy: only the first animates); (re)launch every simulator whose role changed or is new
    for i, s in enumerate(ctx.sims):
        want = roles[i]
        if s.get("role") != want:
            ctx.guest.call(["prepare", "--udids", s["udid"], "--roles", want] + (["--install"] if not s.get("installed") else []) +
                           (["--quiesce"] if ctx.a.quiesce else []), 300)
            s["role"], s["installed"] = want, True
    return boot


def settle(ctx):
    r = ctx.guest.call(["settle", "--max-s", ctx.a.settle_max, "--bg-pct", ctx.a.settle_bg, "--step", ctx.a.settle_step], ctx.a.settle_max + 120)
    return r


def window(ctx, secs, mix, no_strip=False):
    """One measurement window over every simulator of ctx.sims: the guest measures, the host samples the same window.
    Returns (guest result, host system dict, window start/end in guest time)."""
    a = ctx.a
    udids = ",".join(s["udid"] for s in ctx.sims)
    roles = ",".join(s["role"] for s in ctx.sims)
    lead = a.lead if a.lead is not None else 8 + 2 * len(ctx.sims)
    t_go = time.time() + ctx.offset + lead
    ctx.run.need(secs + lead + 60, "a %d s window" % secs)
    args = ["measure", "--udids", udids, "--roles", roles, "--mix", mix, "--secs", secs, "--start-at", "%.3f" % t_go,
            "--probe-method", ctx.probe_method or "openurl", "--probe-every", a.probe_every, "--agent-period", a.agent_period, "--probe-timeout", a.probe_timeout]
    if a.probe_cmd:
        args += ["--probe-cmd", a.probe_cmd]
    pend = ctx.guest.start(args, secs + lead + 240)
    host_go = t_go - ctx.offset
    time.sleep(max(0, host_go - time.time()))
    s0 = ctx.host.snap()
    time.sleep(max(0, secs - (time.time() - host_go)))
    s1 = ctx.host.snap()
    g = pend.wait()
    return g, s0, s1, t_go


def system_dict(ctx, g, s0, s1, frames_total):
    dt = max(s1["mono"] - s0["mono"], 0.001)
    th = hostprobe.thread_delta(s0["threads"], s1["threads"], dt, getattr(ctx.host, "hz", 100))
    gpu_ns = None if s0["gpu"] is None or s1["gpu"] is None else s1["gpu"] - s0["gpu"]
    gu = g["guest"]
    return {
        "qemu_cpu_cores": th["cores"], "qemu_user_cores": th["user"], "qemu_guest_cores": th["guest"], "qemu_sys_cores": th["sys"],
        "qemu_top_threads": th["top"],
        "gpu_busy_pct": None if gpu_ns is None else round(100.0 * gpu_ns / 1e9 / dt, 1),
        "gpu_ms_per_frame": None if gpu_ns is None or not frames_total else round(gpu_ns / 1e6 / frames_total, 2),
        "window_s": round(dt, 2),
        "guest": {"mem_used_mb": gu["mem"]["used_mb"], "mem_free_mb": gu["mem"]["free_mb"], "compressed_mb": gu["mem"]["compressed_mb"],
                  "swapins_d": gu["swapins_d"], "swapouts_d": gu["swapouts_d"], "pageouts_d": gu["pageouts_d"],
                  "pressure_max": gu["pressure_max"], "total_cpu_pct": gu["total_cpu_pct"], "sims_cpu_pct": gu["sims_cpu_pct"],
                  "other_cpu_pct": gu["other_cpu_pct"], "sims_rss_mb": gu["sims_rss_mb"]},
        "host": hostprobe.mem_delta(s0["mem"], s1["mem"], dt),
    }


def build_step(ctx, n, mix, g, s0, s1, t_go, boot=None):
    """The step record: per simulator metrics, system numbers, stability."""
    a = ctx.a
    secs = g["secs"]
    t_end = t_go + a.secs
    sims, frames_total = [], 0
    for i, s in enumerate(g["sims"]):
        role = s["role"]
        raw = [r for r in analysis.parse_raw(s["raw"]) if r[0] >= t_go - 0.001 and r[0] <= t_end + 0.5] or analysis.parse_raw(s["raw"])[:1]
        inj = [(x[0], x[1]) for x in s["injections"] if x[2] == 0 or True]
        if role == "agent":
            sm = {"idx": i, "role": role}
            a_ = analysis.strip.analyze_counters([(t, c) for t, c, _ in raw])
            sm.update(analysis.motion_metrics([t for t, _ in a_["unique"]], a.nominal_fps))
            sm["strip"] = {k: a_[k] for k in ("repeats", "lost", "backwards", "jumps", "unreadable", "span_ticks")}
            ok, why = analysis.strip.strip_gate(a_)
            sm["strip_gate"] = {"ok": ok, "why": why}
            sm.update(analysis.agent_metrics(s["actions"], raw))
        else:
            ticks = None
            if role == "busy" and s["pace"] and raw:
                ps = analysis.pace_summary(s["pace"], raw[0][0], raw[-1][0], t_go, t_end)
                ticks = ps["app_ticks"]
            sm = analysis.sim_metrics(i, role, raw, inj, a.nominal_fps, a.probe_timeout, ticks)
            if role == "busy" and s["pace"] and raw:
                sm["app_tick_fps"] = ps["app_tick_fps"]
        if role in ("busy", "agent") and sm.get("frames"):
            frames_total += sm["frames"]
        if s.get("simfps"):
            sm["surface_fps"] = s["simfps"].get("fps")
        if s.get("simfps_error"):
            sm["observer_error"] = s["simfps_error"]
        p = s.get("proc") or {}
        sm["guest_cpu_pct"], sm["guest_rss_mb"] = p.get("cpu_pct"), p.get("rss_mb")
        if sm.get("longest_gap_s") is not None and role == "static":
            sm["longest_gap_s"] = None
        sims.append(sm)
    system = system_dict(ctx, g, s0, s1, frames_total)
    died = [s["idx"] for s in sims if g["sims"][s["idx"]].get("state_after") != "Booted"]
    kern, knote = ctx.host.kernel_gpu(s0["wall"])
    stab = {"new_ips": len(g["new_ips"]), "ips": g["new_ips"][:5], "sims_died": died, "kernel_gpu_lines": kern, "kernel_note": knote,
            "qemu_same": ctx.host.same_qemu(), "observer_errors": [s["idx"] for s in sims if s.get("observer_error")]}
    stab["ok"] = not (stab["new_ips"] or died or kern or not stab["qemu_same"])
    return {"n": n, "mix": mix, "sims": sims, "system": system, "stability": stab, "boot": boot or {}}


def step_gates(ctx, step, shots=None):
    gs = {"strip": gates.strip_gate_of(step["sims"]), "kernel_gpu": gates.kernel_gate(step["stability"]["kernel_gpu_lines"], step["stability"]["kernel_note"])}
    gs.update(ctx.coh.check(ctx.marks["coh"]))
    if shots:
        gs.update(shots)
    swapped = step["system"]["guest"]["swapouts_d"] or 0
    hs = step["system"]["host"]
    host_swap = (hs.get("cgroup_swap_delta_mb") or 0) > 50 or (hs.get("pswpout_pages") or 0) > 25600
    gs["memory"] = gates.gate("FAIL" if swapped > 0 and ctx.a.fail_on_swap or host_swap else "PASS",
                              "guest swapouts +%s, pressure max %s; host cgroup swap %s MB, pswpout %s pages" % (
                                  swapped, step["system"]["guest"]["pressure_max"], hs.get("cgroup_swap_delta_mb"), hs.get("pswpout_pages")))
    step["gates"] = gs
    step["gates_ok"] = gates.worst(gs)
    step["stable"] = step["stability"]["ok"]
    return gs


def take_shots(ctx, count=2):
    """Screenshots of every simulator, through the guest, judged by the screenshot gates."""
    udids = ",".join(s["udid"] for s in ctx.sims)
    r = ctx.guest.call(["shots", "--udids", udids, "--count", count], 60 + 30 * len(ctx.sims))
    by = []
    for s, one in zip(ctx.sims, r["sims"]):
        by.append({"idx": s["idx"], "role": s["role"], "shots": one["shots"]})
    return gates.shots_gates(by, ctx.golden), by
