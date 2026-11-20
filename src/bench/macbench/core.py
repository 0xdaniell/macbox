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
        self.stock = False          # --stock-devices: fresh stock devices instead of clones of the seed
        self.seed = None            # the guest's answer to `seed`
        self.prefix = "bench"
        self.swap0 = None           # the guest's swap-out counter when the run began
        self.in_window = False      # a measurement window is running: the monitors keep off the guest
        self.taken = False          # the bench's set is owned by this run (lock taken, cleanup registered)
        self.idle_guard = set()     # what sim-idle-guard keep answered for the bench's simulators: kept / absent

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
    ctx.doc["env"].setdefault("guest", {}).update(g)          # keeps what setup_guest recorded (runtime, device type, seed)
    ctx.doc["env"].update({"build_id": ctx.a.build_id or "unknown", "host": host,
                           "switches": sw, "qemu_env": qenv, "tags": dict(kv.split("=", 1) for kv in (ctx.a.tag or []) if "=" in kv),
                           "dry": bool(ctx.host.dry)})


def disk_reason(ctx, floor_gib, what, label=""):
    """None when the filesystem that holds the VM's storage has `floor_gib` free; else why not. A reading that fails refuses
    (a guard that cannot see the disk is not a guard): that is what let the 2026-11-16 outage through."""
    df = ctx.host.disk_free_gb(ctx.a.disk_path or "/")
    if df is None:
        return "free disk space of the VM's storage (--disk-path) cannot be read: refusing to %s %s" % (what, label)
    if df < floor_gib:
        return "free disk %.1f GiB is under the %g GiB floor to %s %s" % (df, floor_gib, what, label)
    return None


def guard(ctx, k_new, label):
    """Refuses to boot k_new more simulators when the host or the guest would run short: returns a reason or None. Checked
    before every boot (and by the monitor while waiting). The sweep stops there; it never swaps the VM to death.
    Host: free disk (>= --disk-boot-gib to boot another), available memory. Guest: available memory after the new
    simulators, memory pressure, swap-outs since the run began (must not grow), the compressor's size, simulators that
    are not the bench's."""
    a = ctx.a
    hm = ctx.host.mem_avail_gb()
    if hm is not None and hm - k_new * 0.5 < a.min_host_free_gib:
        return "host free memory %.1f GiB below the %.1f GiB floor %s" % (hm, a.min_host_free_gib, label)
    why = disk_reason(ctx, a.disk_boot_gib, "boot another simulator", label)
    if why:
        return why
    m = ctx.guest.call(["memcheck"], 60)
    avail = m["mem"].get("avail_mb", m["mem"]["free_mb"])          # free + speculative + inactive + purgeable: what a boot can take
    free_gib = (avail - k_new * ctx.est_sim_mb) / 1024.0
    if free_gib < a.min_guest_free_gib:
        return "guest available memory would be %.1f GiB (%.1f now, %d x ~%d MB to boot), below the %.1f GiB floor %s" % (
            free_gib, avail / 1024.0, k_new, ctx.est_sim_mb, a.min_guest_free_gib, label)
    if (m.get("pressure") or 1) >= 2:
        return "guest memory pressure level %s %s" % (m.get("pressure"), label)
    mem_why = guest_memory_reason(ctx, m["mem"])
    if mem_why:
        return "%s %s" % (mem_why, label)
    if m.get("foreign_booted", 0) > 0 and not a.allow_foreign:
        return "%d simulator(s) outside the bench are booted (the pool or someone's lease): not a clean measurement; --allow-foreign to go on" % m["foreign_booted"]
    return None


def guest_memory_reason(ctx, mem):
    """Swap-outs since the run began (the guest started paging memory out: the simulators no longer fit) and the compressor's size."""
    base = ctx.swap0
    if base is not None and mem.get("swapouts", 0) > base:
        return "guest swap-outs grew from %d to %d since the run began" % (base, mem["swapouts"])
    if mem.get("compressed_mb", 0) > ctx.a.max_compressor_mb:
        return "guest compressor holds %.0f MB (limit %g MB)" % (mem["compressed_mb"], ctx.a.max_compressor_mb)
    return None


def start_reason(ctx):
    """Before the first simulator of a run: the host disk has --disk-start-gib free."""
    return disk_reason(ctx, ctx.a.disk_start_gib, "start a run that boots simulators", "")


def monitor(ctx):
    ctx.run.watch("guards", monitor_check(ctx), ctx.a.watch_every)


def monitor_check(ctx):
    """While the run waits (a boot, a settle, a window): the host's disk, QEMU and, outside measurement windows (their
    own calls would be observer load), the Mac. Any breach aborts the run NOW: the monitor thread writes the partial
    result, runs the one cleanup and exits (8 disk, 9 VM, 7 someone else's simulator, 10 guest memory)."""
    a, st = ctx.a, {"dead": 0, "foreign": 0, "last_guest": 0.0}

    def check():
        why = disk_reason(ctx, a.disk_abort_gib, "go on", "")
        if why:
            return "%s; deleting the bench's simulators" % why, 8
        if not ctx.host.same_qemu():
            return "the VM's QEMU process is gone or was restarted", 9
        now = time.monotonic()
        if ctx.in_window or not ctx.taken or now - st["last_guest"] < a.watch_guest_every:
            return None
        st["last_guest"] = now
        if not ctx.guest.alive():
            st["dead"] += 1
            if st["dead"] >= 2:
                return "the VM stopped answering over ssh (two checks in a row)", 9
            return None
        st["dead"] = 0
        m = ctx.guest.quick(["memcheck"], 60)
        if m.get("foreign_booted", 0) > 0 and not a.allow_foreign:
            st["foreign"] += 1              # the bench's own boots and shutdowns race the count: two polls in a row
            if st["foreign"] >= 2:
                return "a simulator that is not the bench's is booted (someone needs the Mac): yielding", 7
        else:
            st["foreign"] = 0
        why = guest_memory_reason(ctx, m["mem"])
        if why:
            return why, 10
        return None
    return check


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


def teardown(ctx):
    """The cleanup of the bench's simulators. Raises, naming what is left, when anything could not be deleted: the
    cleanup section must never say 'done' on a cleanup that was not verified."""
    info = ctx.guest.call(["teardown"], 300)
    ctx.cleanup_info.update(info)
    if info.get("failed") or info.get("left") or info.get("booted"):
        raise RuntimeError("could not clean up the bench's device set: %s; %d device(s) left, %d booted: run `macbox bench cleanup` "
                           "(the next run refuses to start until it is clean)" % ("; ".join(info.get("failed") or ["nothing reported"])[:300], info.get("left", 0), info.get("booted", 0)))


def keep_minutes(ctx):
    """How long the Mac's idle guard must leave the bench's simulators alone: the run's deadline plus 5 minutes, at most 240."""
    return max(5, min(240, int(ctx.a.deadline / 60.0) + 5))


def setup_guest(ctx, sims=True):
    """Device set, device type, tools check. Returns the guest info. With sims=False the run boots no simulator of its
    own (idle, build): the set is not taken and nothing is registered for cleanup."""
    a = ctx.a
    info = ctx.guest.call(["info"], 120)
    if not info.get("has_simfps") or not info.get("has_app"):
        raise Stop("the bench tools are not built in the guest: macbox bench tools")
    ctx.guest.call(["init-set"], 60)
    if not sims:
        _run_mark(ctx)
        return info
    why = start_reason(ctx)
    if why:
        raise Stop(why)
    acq = ctx.guest.call(["acquire", "--run", ctx.doc["id"]], 60)
    if not acq.get("ok"):
        # refuse BEFORE the cleanup is registered: the leftovers are not this run's to delete (macbox bench cleanup does)
        raise Stop("an earlier run left the bench's device set unclean (%s): run `macbox bench cleanup`, then start again" % "; ".join(acq.get("leftovers", [])[:4]))
    ctx.run.teardown.append(("bench simulators", lambda: teardown(ctx)))
    ctx.taken = True
    t = ctx.guest.call(["ensure-type", "--scale", str(a.scale_x)] + (["--allow-restart"] if a.allow_restart else []), 180)
    if t.get("needs_restart"):
        raise Stop("device type %s was just created and CoreSimulatorService must restart to load it, which would shut "
                   "down every booted simulator: stop them, then run again with --allow-restart (or `macbox pool create`)" % t["type"])
    ctx.type_id, ctx.runtime = t["type"], t["runtime"]
    base = "stock %s" % t["stock_name"] if a.scale_x == 3 else "macbox %dx of %s" % (a.scale_x, t["stock_name"])
    g = ctx.doc["env"]["guest"]
    g["runtime_used"] = t.get("runtime_version")
    g["device_type"] = base + (" (fresh stock devices)" if a.stock_devices else " (production seed)")
    g["device_source"] = "stock devices" if a.stock_devices else "clones of a seed made from a Simlock source device"
    ctx.stock = bool(a.stock_devices)
    ctx.prefix = "bench-%s" % ctx.doc["id"]
    _run_mark(ctx)
    if not ctx.stock:
        make_seed(ctx)
    return info


def make_seed(ctx):
    """The production seed: a settled Slim clone of Simlock's own source device of this type, kept in the bench's set
    (reused when it is there). Every bench simulator is a clone of it. Fails closed: no source, no Slim, no settle, no run."""
    a = ctx.a
    reason = guard(ctx, 1, "before the seed")
    if reason:
        raise Stop(reason)
    args = ["seed", "--type", ctx.type_id, "--keep-min", keep_minutes(ctx), "--settle-max", a.seed_settle_max, "--step", a.settle_step]
    for flag, val in (("--source-set", a.source_set), ("--simslim", a.simslim), ("--slim-profile", a.slim_profile)):
        if val:
            args += [flag, val]
    if a.rebuild_seed:
        args.append("--rebuild")
    r = ctx.guest.call(args, a.seed_settle_max + 2400)
    if not r.get("seed"):
        raise Stop("%s. A bench simulator must be a production simulator (a clone of a Simlock source device with the Slim disables), "
                   "so there is nothing to run on. For a device type Simlock has no source for, or a scenario that needs a stock device, "
                   "use --stock-devices: it creates fresh stock devices (about 1.8 GB of guest disk each, the host-disk guard checks "
                   "before each) and the result says so" % r.get("no_source", "no seed"))
    ctx.seed = r
    ctx.runtime = r.get("runtime") or ctx.runtime
    g = ctx.doc["env"]["guest"]
    g["runtime_used"] = r.get("runtime_version") or g.get("runtime_used")
    g["seed"] = {k: r.get(k) for k in ("source_type", "reused", "made_s", "services_off", "settle_s", "settle_bg_pct", "icons")}
    ctx.section("seed", "INFO", "SEED %s: %s services off (Slim), %s" % (
        "reused" if r.get("reused") else "made from a Simlock source device in %s s (simslim %s s, settled in %s s at %s%% background)" % (
            r.get("made_s"), r.get("slimmed_s"), r.get("settle_s"), r.get("settle_bg_pct")),
        r.get("services_off"), "kept shut down between runs" if not r.get("reused") else "settled when it was made"))


def _run_mark(ctx):
    ctx.swap0 = ctx.guest.call(["memcheck"], 60)["mem"].get("swapouts", 0)           # swap-outs may not grow from here on
    ctx.offset = ctx.guest.clock_offset()
    ctx.run_mark = {"coh": ctx.coh.mark(), "t0": time.time(), "ips": set(ctx.guest.call(["ips"], 60)["ips"])}


def grow(ctx, n, roles, install=True, parallel=False):
    """Make and boot simulators up to n (the ones already running stay), launch each with its role. Returns boot timings.
    Production path: clones of the seed (no disk cost, the seed's Slim disables copied to each). --stock-devices: fresh
    stock devices, one at a time with the guard (host disk, guest memory) in front of each. parallel=True boots all the
    new ones at once (the lifecycle's boot storm); otherwise they boot one after the other with the guard between."""
    have = len(ctx.sims)
    new = n - have
    boot = {}
    if new > 0:
        keep = keep_minutes(ctx)
        done, usable = [], []
        t0 = time.time()
        if parallel and not ctx.stock:
            r = ctx.guest.call(["clone", "--seed", ctx.seed["seed"], "--n", new, "--start", have, "--prefix", ctx.prefix, "--keep-min", keep], 240 + 60 * new)
            fresh = r["sims"]
            b = ctx.guest.call(["boot", "--udids", ",".join(x["udid"] for x in fresh)] + ["--parallel"], 900)
            groups = [(fresh, b)]
        else:
            groups = []
            for k in range(new):
                if k:
                    reason = guard(ctx, 1, "before booting simulator %d" % (have + k + 1))
                    if reason:
                        raise Stop(reason)
                if ctx.stock:
                    r = ctx.guest.call(["create", "--n", 1, "--type", ctx.type_id, "--runtime", ctx.runtime, "--start", have + k, "--keep-min", keep], 240)
                else:
                    r = ctx.guest.call(["clone", "--seed", ctx.seed["seed"], "--n", 1, "--start", have + k, "--prefix", ctx.prefix, "--keep-min", keep], 240)
                groups.append((r["sims"], ctx.guest.call(["boot", "--udids", r["sims"][0]["udid"]], 900)))
        for fresh, b in groups:
            ctx.idle_guard.update(x.get("idle_guard") for x in fresh)
            bad = [s for s in b["sims"] if not s.get("ok")]
            if bad:
                for s_, bs in zip(fresh, b["sims"]):
                    ctx.sims.append({"idx": s_["idx"], "udid": s_["udid"]})          # known to the cleanup and the count even when it failed
                raise Stop("a simulator did not boot: %s" % bad[0].get("err"))
            for s_, bs in zip(fresh, b["sims"]):
                ctx.sims.append({"idx": s_["idx"], "udid": s_["udid"]})
                usable.append(bs["usable_s"])
        together = parallel and not ctx.stock          # stock devices are made and booted one by one even in a storm
        boot = {"new": new, "all_usable_s": groups[0][1]["all_s"] if together else round(time.time() - t0, 2), "each_usable_s": usable,
                "parallel": together}
    # roles may change with N (one-busy: only the first animates); (re)launch every simulator whose role changed or is new
    for i, s in enumerate(ctx.sims):
        want = roles[i]
        if s.get("role") != want:
            ctx.guest.call(["prepare", "--udids", s["udid"], "--roles", want] + (["--install"] if ctx.stock and not s.get("installed") else ["--check-app"]) +
                           (["--quiesce"] if ctx.a.quiesce else []), 300)
            s["role"], s["installed"] = want, True
    return boot


def settle(ctx, at="before the window", max_s=None, bg=None):
    """Waits until the guest is quiet (background CPU under --settle-bg for two samples in a row, up to --settle-max).
    A settle that reaches its cap FAILS the run: a window now would measure whatever keeps the guest busy, not the
    simulators. The top processes by CPU at that moment are recorded in the failure."""
    a = ctx.a
    max_s = a.settle_max if max_s is None else max_s
    bg = a.settle_bg if bg is None else bg
    r = ctx.guest.call(["settle", "--max-s", max_s, "--bg-pct", bg, "--step", a.settle_step, "--consecutive", 2], max_s + 120 + 4 * a.settle_step)
    if not r.get("settled"):
        top = ", ".join("%s %g%%" % (t["cmd"], t["cpu_pct"]) for t in r.get("top", [])[:6]) or "nothing"
        line = "NOT SETTLED %s: the guest's background CPU stayed at %.0f%% of a core (bar %g%%) for %g s; top processes by CPU: %s" % (
            at, r.get("bg_pct", -1), bg, max_s, top)
        ctx.section("settle", "FAIL", line, {"at": at, "series": r.get("series"), "top": r.get("top")})
        raise Stop(line)
    return r


def precheck(ctx, what):
    """Before a measurement window: count the bench's OWN booted simulators and refuse any mismatch with the expected
    number (a simulator the Mac's idle guard or someone shut down would be measured as if it ran); yield when a simulator
    that is not the bench's is booted. The measured count is what the result records."""
    a = ctx.a
    udids = [s["udid"] for s in ctx.sims]
    r = ctx.guest.call(["precheck", "--udids", ",".join(udids), "--keep-min", keep_minutes(ctx)], 90)
    ctx.idle_guard.update(r.get("keep", {}))
    if r.get("foreign_booted") and not a.allow_foreign:
        line = "YIELDING %s: %d simulator(s) outside the bench are booted (someone needs the Mac); --allow-foreign measures anyway" % (what, r["foreign_booted"])
        ctx.section("foreign", "FAIL", line)
        raise Stop(line)
    if r["booted"] != len(udids) or r.get("missing") or r.get("extra"):
        line = "ABORT %s: %d of the bench's simulators are booted, expected %d (%d expected one(s) not booted, %d unexpected): one was shut down under the run (the Mac's idle guard?)" % (
            what, r["booted"], len(udids), len(r.get("missing", [])), len(r.get("extra", [])))
        ctx.section("booted_check", "FAIL", line, {"booted": r["booted"], "expected": len(udids)})
        raise Stop(line)
    return r


def window(ctx, secs, mix, no_strip=False):
    """One measurement window over every simulator of ctx.sims: the guest measures, the host samples the same window.
    Returns (guest result, host system dict, window start/end in guest time)."""
    a = ctx.a
    pre = precheck(ctx, "before the %g s window" % secs)
    udids = ",".join(s["udid"] for s in ctx.sims)
    roles = ",".join(s["role"] for s in ctx.sims)
    lead = a.lead if a.lead is not None else 8 + 2 * len(ctx.sims)
    t_go = time.time() + ctx.offset + lead
    ctx.run.need(secs + lead + 60, "a %d s window" % secs)
    args = ["measure", "--udids", udids, "--roles", roles, "--mix", mix, "--secs", secs, "--start-at", "%.3f" % t_go,
            "--probe-method", ctx.probe_method or "none", "--probe-every", a.probe_every, "--agent-period", a.agent_period, "--probe-timeout", a.probe_timeout]
    if a.probe_cmd:
        args += ["--probe-cmd", a.probe_cmd]
    if no_strip:          # the unviewed screen: no strip, no marker, no injection, so simfps locks no surface
        args[args.index("--probe-method") + 1] = "none"
        args.append("--no-strip")
    ctx.in_window = True
    try:
        pend = ctx.guest.start(args, secs + lead + 240)
        host_go = t_go - ctx.offset
        time.sleep(max(0, host_go - time.time()))
        s0 = ctx.host.snap()
        time.sleep(max(0, secs - (time.time() - host_go)))
        s1 = ctx.host.snap()
        g = pend.wait()
    finally:
        ctx.in_window = False
    g["precheck"] = pre
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
        "guest": {"mem_used_mb": gu["mem"]["used_mb"], "mem_free_mb": gu["mem"]["free_mb"], "mem_avail_mb": gu["mem"].get("avail_mb"), "observer_cpu_pct": gu.get("observer_cpu_pct"), "compressed_mb": gu["mem"]["compressed_mb"],
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
            a_ = analysis.agent_strip(raw, s["actions"])          # per launch: each one restarts the counter
            sm.update(analysis.motion_metrics([t for t, _ in a_["unique"]], a.nominal_fps))
            sm["strip"] = {k: a_[k] for k in ("repeats", "lost", "backwards", "jumps", "unreadable", "span_ticks", "segments")}
            ok, why = analysis.strip.strip_gate(a_)
            sm.update(analysis.agent_metrics(s["actions"], raw))
            sm["frames"] = len(a_["unique"])
            sm["missing_frames"] = a_["missing_frames"]
            if sm["action_failures"] or sm["missing_frames"]:
                ok = False
                why = "agent action failures: %d; launches without frames: %d" % (sm["action_failures"], sm["missing_frames"])
            sm["strip_gate"] = {"ok": ok, "why": why}
        else:
            ticks = None
            if role == "busy" and s["pace"] and raw:
                ps = analysis.pace_summary(s["pace"], raw[0][0], raw[-1][0], t_go, t_end)
                ticks = ps["app_ticks"]
            sm = analysis.sim_metrics(i, role, raw, inj, a.nominal_fps, a.probe_timeout, ticks, window=(t_go - 0.001, t_end))
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
    foreign = max((g.get("foreign_booted") or {}).values() or [0])
    judged, listed = gates.crash_split(g["new_ips"], crash_names(a), getattr(a, "strict_crashes", False))
    stab = {"new_ips": len(judged), "ips": judged[:5], "other_ips": listed[:10], "sims_died": died, "kernel_gpu_lines": kern, "kernel_note": knote,
            "qemu_same": ctx.host.same_qemu(), "observer_errors": [s["idx"] for s in sims if s.get("observer_error")],
            "foreign_booted": foreign}
    stab["ok"] = not (stab["new_ips"] or died or kern or not stab["qemu_same"] or (foreign and not a.allow_foreign))
    pre = g.get("precheck") or {}
    # "booted" is what the guest counted in the bench's set right before the window, never the expected number
    return {"n": n, "mix": mix, "booted": pre.get("booted"), "expected_booted": len(ctx.sims), "sims": sims, "system": system,
            "stability": stab, "boot": boot or {}, "counter": counter_gate(g["sims"], mix)}


def counter_gate(gsims, mix):
    """A frame counter that returns nothing or an error for a simulator fails the window: every simulator of a window with
    a counter must have an answer from simfps, and an animating one must have read counters from its surfaces."""
    if mix == "idle":
        return gates.gate("SKIP", "no counter in the idle mix")
    bad = []
    for i, s in enumerate(gsims):
        if s.get("simfps_error") or not s.get("simfps"):
            bad.append("sim %d: %s" % (i, s.get("simfps_error") or "simfps returned nothing"))
        elif s.get("role") in ("busy", "agent"):
            rd = [r for r in analysis.parse_raw(s.get("raw") or "") if 0 <= r[1] < 65535]
            if len(rd) < 2:
                bad.append("sim %d: no frame counter read from its surfaces (%d reads)" % (i, len(rd)))
    if bad:
        return gates.gate("FAIL", "; ".join(bad[:4]))
    return gates.gate("PASS", "simfps answered for %d simulator(s), counters read" % len(gsims))


def step_gates(ctx, step, shots=None):
    gs = {"counter": step["counter"], "strip": gates.strip_gate_of(step["sims"]), "kernel_gpu": gates.kernel_gate(step["stability"]["kernel_gpu_lines"], step["stability"]["kernel_note"])}
    gs.update(ctx.coh.check(ctx.marks["coh"]))
    if shots:
        gs.update(shots)
    swapped = step["system"]["guest"]["swapouts_d"] or 0
    hs = step["system"]["host"]
    cg = hs.get("cgroup_swap_delta_mb")          # the VM's own cgroup: other projects' swapping on the host is not this run's
    host_swap = cg is not None and cg > 50
    guest_swap = swapped > 0 and ctx.a.fail_on_swap
    gs["memory"] = gates.gate("FAIL" if guest_swap or host_swap else ("PASS" if cg is not None else "SKIP"),
                              "guest swapouts +%s, pressure max %s; host cgroup swap %s MB, pswpout %s pages" % (
                                  swapped, step["system"]["guest"]["pressure_max"], hs.get("cgroup_swap_delta_mb"), hs.get("pswpout_pages")))
    step["gates"] = gs
    step["gates_ok"] = gates.worst(gs)
    step["stable"] = step["stability"]["ok"]
    return gs


def crash_names(a):
    """Extra process names whose crash report fails a run (--crash-names a,b)."""
    return [n for n in (getattr(a, "crash_names", None) or "").split(",") if n.strip()]


def run_gates(ctx):
    """The gates that hold for a whole run, whatever the scenario: Coherent and QEMU's log since the run began, kernel
    GPU error lines since then, and crash reports that appeared on the guest. A scenario that stopped before its setup
    has nothing to judge. Never raises: a gate that cannot be read is SKIP."""
    mk = getattr(ctx, "run_mark", None)
    if not mk:
        return
    gs = {}
    try:
        for k, v in ctx.coh.check(mk["coh"]).items():
            gs["run_" + k] = v
    except Exception as e:
        gs["run_coherent"] = gates.gate("SKIP", "unreadable: %s" % type(e).__name__)
    try:
        kern, note = ctx.host.kernel_gpu(mk["t0"])
        gs["run_kernel_gpu"] = gates.kernel_gate(kern, note)
    except Exception as e:
        gs["run_kernel_gpu"] = gates.gate("SKIP", "unreadable: %s" % type(e).__name__)
    try:
        now = set(ctx.guest.call(["ips"], 60)["ips"])
        gs["run_crash_reports"] = gates.crash_gate(now - mk["ips"], crash_names(ctx.a), getattr(ctx.a, "strict_crashes", False))
    except Exception as e:
        gs["run_crash_reports"] = gates.gate("SKIP", "guest unreachable: %s" % type(e).__name__)
    ctx.doc.setdefault("gates", {}).update(gs)
    for name, v in gs.items():
        ctx.section("gate:" + name, v["status"], "GATE %s %s: %s" % (v["status"], name, v["detail"]))


def take_shots(ctx, count=2, judge_static=False):
    """Screenshots of every simulator, through the guest, judged by the screenshot gates. Static screens are judged
    for pixels only when `judge_static`: after a window with input probes their marker state is not the golden's
    (the pixel gate runs on a freshly launched static app, see scale.golden_phase)."""
    udids = ",".join(s["udid"] for s in ctx.sims)
    r = ctx.guest.call(["shots", "--udids", udids, "--count", count], 60 + 30 * len(ctx.sims))
    by = []
    for s, one in zip(ctx.sims, r["sims"]):
        role = s["role"] if (s["role"] != "static" or judge_static) else "static-unjudged"
        by.append({"idx": s["idx"], "role": role, "shots": one["shots"]})
    return gates.shots_gates(by, ctx.golden), by
