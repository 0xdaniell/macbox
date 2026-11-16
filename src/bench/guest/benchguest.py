#!/usr/bin/env python3
"""benchguest: the bench's helper inside macOS (run by `macbox bench`; `xcrun python3`, standard library only,
Python 3.9 compatible). Every subcommand prints ONE JSON object on its last stdout line.

Every simulator command goes through simctl() below, which always passes --set <the bench's own device set>
and refuses a set without the bench's marker file: the pool and Simlock's devices are never touched.

  info | init-set | ensure-type | create | boot | prepare | settle | calibrate | measure | shots |
  cycle | teardown | memcheck | build
"""
import argparse
import base64
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
BUNDLE_ID = "dev.macbox.glassbench"
MARKER = ".macbox-bench-set"
PROBE_POINT = "36,175"          # the input marker's centre, in points (GlassBench -probe)
SET = None
STUB = os.environ.get("BENCHGUEST_STUB")
CALLS = []


# --- primitives (replaced by benchguest_stub.py under BENCHGUEST_STUB) ---------------------------------
def run(argv, timeout=120, env=None):
    """(returncode, stdout, stderr, seconds). A timeout kills the process and returns rc 124."""
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr, time.time() - t0
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout", time.time() - t0
    except OSError as e:
        return 127, "", str(e), time.time() - t0


def spawn_simfps(udid, secs, label, strip, start_at, raw_path):
    """Starts simfps; returns an object whose .wait(timeout) -> (json dict or None, error or None)."""
    env = dict(os.environ, SIMFPS_SET=SET, SIMFPS_START_AT="%.3f" % start_at, SIMFPS_STAMPS=raw_path,
               SIMFPS_PROBE=PROBE_POINT)
    argv = [os.path.join(HERE, "..", "simfps"), udid, str(secs), label] + (["--strip"] if strip else [])
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    class H:
        def wait(self, timeout):
            try:
                out, err = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
                return None, "simfps hung (killed after %ds)" % timeout
            try:
                return json.loads(out.strip().splitlines()[-1]), None
            except (ValueError, IndexError):
                return None, (err or out or "no output").strip()[-200:]

        def kill(self):
            try:
                p.kill()
            except OSError:
                pass
    return H()


def ps_text():
    return run(["ps", "-axww", "-o", "pid=,ppid=,rss=,time=,command="], 30)[1]


def vmstat_text():
    return run(["vm_stat"], 15)[1]


def sysctl(name):
    return run(["sysctl", "-n", name], 15)[1].strip()


def now():
    return time.time()



# --- simctl ----------------------------------------------------------------------------------------
class GuestError(Exception):
    pass


def check_set():
    if not SET:
        raise GuestError("--set is required")
    if not os.path.exists(os.path.join(SET, MARKER)):
        raise GuestError("not a bench device set (no %s): refusing" % MARKER)


def simctl(*args, timeout=180):
    """xcrun simctl --set <bench set> ...; the only way this file starts simctl."""
    check_set()
    argv = ["xcrun", "simctl", "--set", SET] + [str(a) for a in args]
    CALLS.append(argv)
    return run(argv, timeout)


def simctl_json(*args):
    rc, out, err, _ = simctl(*args, "-j")
    if rc != 0:
        raise GuestError("simctl %s: %s" % (" ".join(map(str, args)), err.strip()[:200]))
    return json.loads(out)


def devices():
    out = []
    for rt, devs in simctl_json("list", "devices").get("devices", {}).items():
        for d in devs:
            d = dict(d)
            d["runtime"] = rt
            out.append(d)
    return out


# --- parsers (unit-tested) -------------------------------------------------------------------------
def parse_vm_stat(text):
    """vm_stat -> {"page": bytes, "free": pages, ...}. Keys lower-cased, spaces to underscores."""
    d = {}
    m = re.search(r"page size of (\d+) bytes", text)
    d["page"] = int(m.group(1)) if m else (4096 if os.uname()[4] == "x86_64" else 16384)
    for line in text.splitlines():
        m = re.match(r"^\"?([A-Za-z ()-]+?)\"?:\s+(\d+)\.?\s*$", line)
        if m:
            d[re.sub(r"[^a-z0-9]+", "_", m.group(1).lower()).strip("_")] = int(m.group(2))
    return d


def mem_summary(vm):
    """The fields a report needs, in MB, from parse_vm_stat()."""
    p = vm.get("page", 16384) / 1048576.0
    g = lambda k: vm.get(k, 0)
    used = (g("pages_active") + g("pages_wired_down") + g("pages_occupied_by_compressor")) * p
    return {"free_mb": round((g("pages_free") + g("pages_speculative")) * p, 1), "used_mb": round(used, 1),
            # what the guest can hand out without swapping: free, speculative, inactive (file cache) and purgeable pages
            "avail_mb": round((g("pages_free") + g("pages_speculative") + g("pages_inactive") + g("pages_purgeable")) * p, 1),
            "compressed_mb": round(g("pages_occupied_by_compressor") * p, 1),
            "swapins": g("swapins"), "swapouts": g("swapouts"), "pageins": g("pageins"), "pageouts": g("pageouts")}


def parse_cputime(s):
    """ps TIME: [d-][h:]m:s[.cc] -> seconds"""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = s.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return days * 86400 + secs


def parse_ps(text):
    """[{pid, ppid, rss_kb, cpu_s, cmd}]"""
    out = []
    for line in text.splitlines():
        p = line.split(None, 4)
        if len(p) < 5:
            continue
        try:
            out.append({"pid": int(p[0]), "ppid": int(p[1]), "rss_kb": int(p[2]), "cpu_s": parse_cputime(p[3]), "cmd": p[4]})
        except ValueError:
            pass
    return out


def own_pids(procs, me=None):
    """The helper itself, its ancestors (ssh, shells) and everything it started (simfps, simctl, ps): the observer."""
    me = os.getpid() if me is None else me
    by_pid = {p["pid"]: p for p in procs}
    by_ppid = {}
    for p in procs:
        by_ppid.setdefault(p["ppid"], []).append(p["pid"])
    mine, stack = {me}, [me]
    while stack:
        for c in by_ppid.get(stack.pop(), []):
            if c not in mine:
                mine.add(c)
                stack.append(c)
    q = me
    while q in by_pid and by_pid[q]["ppid"] > 1:
        q = by_pid[q]["ppid"]
        mine.add(q)
    return mine


def sim_trees(procs, udids, exclude=()):
    """{udid: set of pids}: processes whose command line names the simulator's UDID (launchd_sim, SimMetalHost,
    ...) and every descendant of them, never the observer (`exclude`: the helper's own tree, which names every
    UDID on its command line). A process that names no simulator is nobody's."""
    by_ppid = {}
    for p in procs:
        by_ppid.setdefault(p["ppid"], []).append(p["pid"])
    out = {}
    for u in udids:
        roots = [p["pid"] for p in procs if u in p["cmd"] and p["pid"] not in exclude]
        seen, stack = set(roots), list(roots)
        while stack:
            for c in by_ppid.get(stack.pop(), []):
                if c not in seen and c not in exclude:
                    seen.add(c)
                    stack.append(c)
        out[u] = seen
    return out


def attribute(before, after, udids, secs, exclude=()):
    """Per simulator and in total: CPU % of one core over the window (from the processes' CPU time deltas) and RSS MB
    at the end. A process that started or ended inside the window counts from its first sample or not at all:
    short-lived processes are undercounted (declared in the README)."""
    b = {p["pid"]: p for p in before}
    trees = sim_trees(after, udids, exclude)
    per = {}
    for u in udids:
        cpu = sum(max(0.0, p["cpu_s"] - b[p["pid"]]["cpu_s"]) for p in after if p["pid"] in trees[u] and p["pid"] in b)
        rss = sum(p["rss_kb"] for p in after if p["pid"] in trees[u]) / 1024.0
        per[u] = {"cpu_pct": round(100 * cpu / secs, 1), "rss_mb": round(rss, 1), "procs": len(trees[u])}
    obs = sum(max(0.0, p["cpu_s"] - b[p["pid"]]["cpu_s"]) for p in after if p["pid"] in b and p["pid"] in exclude)
    tot = sum(max(0.0, p["cpu_s"] - b[p["pid"]]["cpu_s"]) for p in after if p["pid"] in b and p["pid"] not in exclude)
    mine = set().union(*trees.values()) if trees else set()
    sims_cpu = sum(max(0.0, p["cpu_s"] - b[p["pid"]]["cpu_s"]) for p in after if p["pid"] in mine and p["pid"] in b)
    return per, {"observer_cpu_pct": round(100 * obs / secs, 1), "total_cpu_pct": round(100 * tot / secs, 1), "sims_cpu_pct": round(100 * sims_cpu / secs, 1),
                 "other_cpu_pct": round(100 * (tot - sims_cpu) / secs, 1),
                 "sims_rss_mb": round(sum(v["rss_mb"] for v in per.values()), 1)}


def pressure_level():
    s = sysctl("kern.memorystatus_vm_pressure_level")
    return int(s) if s.isdigit() else None


def parse_pace(text):
    """GlassBench's Documents/pace.json (one JSON object per line) -> [(at, counter, fps, ticks)]"""
    out = []
    for line in text.splitlines():
        try:
            d = json.loads(line)
            out.append((float(d["at"]), int(d["counter"]), float(d.get("fps", 0)), int(d.get("ticks", 0))))
        except (ValueError, KeyError, TypeError):
            pass
    return out


def read_pace(udid):
    rc, out, _, _ = simctl("get_app_container", udid, BUNDLE_ID, "data")
    if rc != 0:
        return []
    try:
        with open(os.path.join(out.strip(), "Documents", "pace.json")) as f:
            return parse_pace(f.read())
    except OSError:
        return []


def list_ips():
    names = set()
    for d in (os.path.expanduser("~/Library/Logs/DiagnosticReports"), "/Library/Logs/DiagnosticReports"):
        try:
            for n in os.listdir(d):
                if n.endswith((".ips", ".crash")):
                    names.add(n)
        except OSError:
            pass
    return names


# --- injection -------------------------------------------------------------------------------------
def inject(method, udid, state, cmd_tpl=None):
    """One input event; returns the command's rc. state: a per-simulator dict for alternating methods."""
    if method == "openurl":
        return simctl("openurl", udid, "glassbench://toggle", timeout=20)[0]
    if method == "appearance":
        state["dark"] = not state.get("dark", False)
        return simctl("ui", udid, "appearance", "dark" if state["dark"] else "light", timeout=20)[0]
    if method == "cmd" and cmd_tpl:
        return run(["/bin/sh", "-c", cmd_tpl.replace("{udid}", udid)], 20)[0]
    return 2


def probe_loop(udid, method, cmd_tpl, t_first, period, t_stop, out, stop_evt):
    state = {}
    t = t_first
    while t < t_stop and not stop_evt.is_set():
        d = t - now()
        if d > 0:
            stop_evt.wait(d)
            if stop_evt.is_set():
                break
        t0 = now()
        rc = inject(method, udid, state, cmd_tpl)
        out.append([t0, now(), rc])
        t += period


# --- actions of the agent mix ----------------------------------------------------------------------
def agent_loop(udid, idx, n, t_go, t_stop, period, out, stop_evt):
    """launch app -> scroll (the app animates by itself) -> screenshot -> terminate -> idle, staggered by sim."""
    t = t_go + (idx * period / max(n, 1))
    tmp = tempfile.mkdtemp(prefix="benchshot")
    try:
        while t + period <= t_stop + 0.5 and not stop_evt.is_set():
            d = t - now()
            if d > 0 and stop_evt.wait(d):
                break
            t0 = now()
            rc = simctl("launch", udid, BUNDLE_ID, "-probe", timeout=60)[0]
            out.append({"op": "launch", "t0": t0, "t1": now(), "rc": rc})
            stop_evt.wait(0.45 * period)
            t0 = now()
            rc = simctl("io", udid, "screenshot", "--type=png", os.path.join(tmp, "s.png"), timeout=60)[0]
            out.append({"op": "screenshot", "t0": t0, "t1": now(), "rc": rc})
            stop_evt.wait(0.15 * period)
            t0 = now()
            rc = simctl("terminate", udid, BUNDLE_ID, timeout=30)[0]
            out.append({"op": "terminate", "t0": t0, "t1": now(), "rc": rc})
            t += period
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- subcommands -----------------------------------------------------------------------------------
def out_json(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def cmd_info(a):
    sw = run(["sw_vers"], 10)[1]
    ver = re.search(r"ProductVersion:\s*(\S+)", sw)
    xc = run(["xcodebuild", "-version"], 30)[1].replace("\n", " ").strip()
    rts = []
    try:
        rts = [{"name": r.get("name"), "version": r.get("version"), "build": r.get("buildversion")}
               for r in json.loads(run(["xcrun", "simctl", "list", "runtimes", "-j"], 30)[1]).get("runtimes", [])
               if r.get("isAvailable", True)]
    except ValueError:
        pass
    vm = parse_vm_stat(vmstat_text())
    out_json({"macos": ver.group(1) if ver else None, "xcode": xc, "runtimes": rts, "arch": run(["uname", "-m"], 5)[1].strip(),
              "cpus": sysctl("hw.ncpu"), "mem_mb": int(sysctl("hw.memsize") or 0) // 1048576, "mem": mem_summary(vm),
              "has_simfps": bool(STUB) or os.path.exists(os.path.join(HERE, "..", "simfps")),
              "has_app": bool(STUB) or os.path.isdir(os.path.join(HERE, "..", "GlassBench.app")), "stub": bool(STUB)})


def cmd_init_set(a):
    global SET
    os.makedirs(a.set, exist_ok=True)
    with open(os.path.join(a.set, MARKER), "w") as f:
        f.write("macbox bench device set\n")
    SET = a.set
    out_json({"ok": True, "devices": len(devices())})


def stock_pro_type():
    """(runtime id, runtime version, 'iPhone NN Pro' type name, id, bundle): macbox-guest's choice of the stock type."""
    best = None
    rts = json.loads(run(["xcrun", "simctl", "list", "runtimes", "-j"], 30)[1]).get("runtimes", [])
    ios = [r for r in rts if r.get("platform", "iOS") == "iOS" and r.get("isAvailable", True)]
    if not ios:
        raise GuestError("no iOS Simulator runtime")
    ios.sort(key=lambda r: [int(x) for x in re.findall(r"\d+", r.get("version", "0"))])
    rt = ios[-1]
    for d in rt.get("supportedDeviceTypes", []):
        m = re.match(r"^iPhone (\d+) Pro$", d["name"])
        if m and d["identifier"].startswith("com.apple.") and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), d)
    if not best:
        raise GuestError("the runtime supports no 'iPhone NN Pro'")
    d = best[1]
    return rt["identifier"], rt.get("version"), d["name"], d["identifier"], d.get("bundlePath", "")


def cmd_ensure_type(a):
    """The device type for a pixel scale: the stock iPhone NN Pro at 3x, a macbox type (the same one `macbox pool`
    makes, same identifier, reused when it exists) at 1x and 2x. Making a NEW type needs CoreSimulatorService to
    restart, which shuts down every simulator: done only with --allow-restart and only when none is booted."""
    rt, rtv, name, stock_id, bundle = stock_pro_type()
    scale = int(a.scale)
    res = {"runtime": rt, "runtime_version": rtv, "stock_name": name, "scale": scale}
    if scale == 3:
        res.update({"type": stock_id, "created": False, "needs_restart": False})
        return out_json(res)
    tid = "dev.macbox.CoreSimulator.SimDeviceType.%s-%dx" % (stock_id.rsplit(".", 1)[-1], scale)
    known = {t["identifier"] for t in json.loads(run(["xcrun", "simctl", "list", "devicetypes", "-j"], 30)[1]).get("devicetypes", [])}
    if tid in known:
        res.update({"type": tid, "created": False, "needs_restart": False})
        return out_json(res)
    types_dir = os.path.expanduser("~/Library/Developer/CoreSimulator/Profiles/DeviceTypes")
    dest = os.path.join(types_dir, "%s (%dx).simdevicetype" % (os.path.basename(bundle)[:-len(".simdevicetype")], scale))
    if not os.path.isdir(bundle):
        raise GuestError("no device type bundle at the stock type's path")
    os.makedirs(types_dir, exist_ok=True)
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(bundle, tmp, symlinks=True)
    pp = os.path.join(tmp, "Contents", "Resources", "profile.plist")
    with open(pp, "rb") as f:
        prof = plistlib.load(f)
    s = int(prof["mainScreenScale"])
    pw, ph = int(prof["mainScreenWidth"]) // s, int(prof["mainScreenHeight"]) // s
    prof["mainScreenScale"] = float(scale) if isinstance(prof["mainScreenScale"], float) else scale
    prof["mainScreenWidth"], prof["mainScreenHeight"] = pw * scale, ph * scale
    prof["mainScreenWidthDPI"] = (int(prof["mainScreenWidthDPI"]) * scale + s // 2) // s
    prof["mainScreenHeightDPI"] = (int(prof["mainScreenHeightDPI"]) * scale + s // 2) // s
    prof.pop("createByDefaultForRuntimeVersions", None)       # else CoreSimulator would make and pair devices of it itself
    prof.pop("pairByDefaultForRuntimeVersions", None)
    with open(pp, "wb") as f:
        plistlib.dump(prof, f)
    ip = os.path.join(tmp, "Contents", "Info.plist")
    with open(ip, "rb") as f:
        info = plistlib.load(f)
    info["CFBundleIdentifier"] = tid
    info["CFBundleName"] = "%s (%dx)" % (name, scale)
    with open(ip, "wb") as f:
        plistlib.dump(info, f)
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(tmp, dest)
    res.update({"type": tid, "created": True, "needs_restart": True})
    if a.allow_restart:
        if count_launchd_sim() or [d for d in devices() if d.get("state") == "Booted"]:
            res["restart"] = "refused: a simulator is running (in any device set)"
        else:
            run(["pkill", "-f", "CoreSimulatorService"], 10)
            time.sleep(3)
            res["restart"] = "done"
            res["needs_restart"] = False
    out_json(res)


def cmd_create(a):
    made = []
    for i in range(a.start, a.start + a.n):
        name = "%s-%d" % (a.prefix, i)
        rc, out, err, secs = simctl("create", name, a.type, a.runtime)
        if rc != 0:
            raise GuestError("create %s: %s" % (name, err.strip()[:200]))
        made.append({"idx": i, "udid": out.strip().splitlines()[-1], "create_s": round(secs, 2)})
    out_json({"sims": made})


def _boot_one(udid, res, i):
    t0 = now()
    rc, _, err, _ = simctl("boot", udid, timeout=300)
    t1 = now()
    ok = rc == 0 or "current state: Booted" in err
    rc2 = simctl("bootstatus", udid, timeout=600)[0] if ok else rc
    res[i] = {"udid": udid, "ok": ok and rc2 == 0, "boot_cmd_s": round(t1 - t0, 2), "usable_s": round(now() - t0, 2),
              "err": None if ok and rc2 == 0 else err.strip()[-120:]}


def slim_count(udid):
    """Services simslim keeps disabled for a simulator (what macbox-guest counts), 0 = stock."""
    f = "/private/var/tmp/com.apple.CoreSimulator.SimDevice.%s/disabled.plist" % udid
    if not os.path.exists(f):
        return 0
    return run(["plutil", "-p", f], 15)[1].count("=> true")


def cmd_slim(a):
    """The pool's Slim configuration on the bench's simulators: runs --cmd ({udid}) for each, then counts the
    services left disabled. HYPOTHESIS: simslim finds devices of a custom device set; the result says."""
    res = []
    for u in a.udids.split(","):
        rc, out, err, secs = run(["/bin/sh", "-c", a.cmd.replace("{udid}", u)], 900)
        res.append({"udid": u, "rc": rc, "s": round(secs, 1), "services_off": slim_count(u), "err": (err or "").strip()[-120:] if rc else None})
    out_json({"sims": res})


def cmd_boot(a):
    us = a.udids.split(",")
    res = [None] * len(us)
    t0 = now()
    if a.parallel:
        th = [threading.Thread(target=_boot_one, args=(u, res, i)) for i, u in enumerate(us)]
        [t.start() for t in th]
        [t.join(700) for t in th]
    else:
        for i, u in enumerate(us):
            _boot_one(u, res, i)
    out_json({"sims": [r or {"ok": False, "err": "no result"} for r in res], "all_s": round(now() - t0, 2)})


def cmd_prepare(a):
    """status bar fixed, app installed, launched with the arguments of the simulator's role."""
    us, roles = a.udids.split(","), a.roles.split(",")
    app = os.path.join(HERE, "..", "GlassBench.app")
    res = []
    for u, role in zip(us, roles):
        t0 = now()
        rc_i = simctl("install", u, app, timeout=120)[0] if a.install else 0
        simctl("status_bar", u, "override", "--time", "9:41", "--batteryState", "charged", "--batteryLevel", "100",
               "--cellularBars", "4", "--wifiBars", "3", "--operatorName", "", timeout=30)
        if a.quiesce:
            simctl("spawn", u, "launchctl", "disable", "system/com.apple.mediaanalysisd", timeout=30)
            simctl("spawn", u, "launchctl", "bootout", "system/com.apple.mediaanalysisd", timeout=30)
        simctl("ui", u, "appearance", "light", timeout=30)
        simctl("terminate", u, BUNDLE_ID, timeout=30)
        args = ["-probe"] + (["-static"] if role == "static" else [])
        rc_l = 0 if role in ("agent", "idle") else simctl("launch", u, BUNDLE_ID, *args, timeout=60)[0]
        res.append({"udid": u, "role": role, "install_rc": rc_i, "launch_rc": rc_l, "s": round(now() - t0, 2)})
    out_json({"sims": res})


def parse_name_cpu(text):
    return text


def bg_cpu_pct(secs=3.0):
    """Everything not part of the load: CPU % of one core used by processes other than the renderers, the app and
    the observer (the settle condition: the guest has stopped doing its own housekeeping)."""
    skip = re.compile(r"(^|/)(SimMetalHost|backboardd|GlassBench|SimRenderServer|kernel_task|simfps|WindowServer|python3?|Python|ps|top|ssh|sshd)( |$)")
    a = parse_ps(ps_text())
    mine = own_pids(a)
    t0 = now()
    time.sleep(secs)
    b = parse_ps(ps_text())
    dt = now() - t0
    am = {p["pid"]: p for p in a}
    tot = sum(max(0.0, p["cpu_s"] - am[p["pid"]]["cpu_s"]) for p in b if p["pid"] in am and p["pid"] not in mine and not skip.search(p["cmd"]))
    return round(100 * tot / dt, 1)


def cmd_settle(a):
    t0, series = now(), []
    while True:
        v = bg_cpu_pct(a.step)
        series.append(v)
        if v < a.bg_pct or now() - t0 > a.max_s:
            break
    out_json({"settled": series[-1] < a.bg_pct, "bg_pct": series[-1], "waited_s": round(now() - t0, 1), "series": series[-8:]})


def run_window(a, udids, roles, mix, secs, t_go, tmp, probe_method, probe_every, cmd_tpl, no_strip=False, probe_timeout=2.0):
    """simfps on each simulator, the probe and agent threads, and the guest's samples around the window."""
    raws, handles, stop_evt = {}, {}, threading.Event()
    if mix != "idle":
        for i, u in enumerate(udids):
            raws[u] = os.path.join(tmp, "raw-%d.txt" % i)
            handles[u] = spawn_simfps(u, secs, "bench-%d" % i, roles[i] != "static" and not no_strip, t_go, raws[u])
    ips0, ps0, vm0, lvl0 = list_ips(), None, None, pressure_level()
    d = t_go - now()
    if d > 0:
        time.sleep(d)
    ps0, vm0 = parse_ps(ps_text()), parse_vm_stat(vmstat_text())
    t_start = now()
    t_stop = t_go + secs
    inj, acts, threads = {u: [] for u in udids}, {u: [] for u in udids}, []
    lvls = []
    if mix in ("all-busy", "one-busy") and probe_method != "none":
        targets = list(udids)          # the busy simulator too: it is what a one-busy fleet's user waits on
        for k, u in enumerate(targets):
            t = threading.Thread(target=probe_loop, args=(u, probe_method, cmd_tpl, t_go + min(1.0, secs * 0.15) + k * probe_every / max(len(targets), 1),
                                                          probe_every, t_stop - probe_timeout - 0.2, inj[u], stop_evt))
            threads.append(t)
    if mix == "agent":
        for i, u in enumerate(udids):
            threads.append(threading.Thread(target=agent_loop, args=(u, i, len(udids), t_go, t_stop, a.agent_period, acts[u], stop_evt)))

    def sample_pressure():
        while not stop_evt.is_set() and now() < t_stop:
            lvls.append(pressure_level())
            stop_evt.wait(2.0)
    threads.append(threading.Thread(target=sample_pressure))
    [t.start() for t in threads]
    res = {}
    try:
        for u, h in handles.items():
            res[u] = h.wait(max(5, t_stop - now() + 40))
        while now() < t_stop:
            time.sleep(0.2)
    finally:
        stop_evt.set()
        [t.join(30) for t in threads]
        for h in handles.values():
            h.kill()
    ps1, vm1 = parse_ps(ps_text()), parse_vm_stat(vmstat_text())
    t_end = now()
    if mix != "idle" and not STUB:
        time.sleep(6)          # GlassBench writes pace.json every 5 s: wait for the record that closes the window
    per, tot = attribute(ps0, ps1, udids, max(t_end - t_start, 0.001), own_pids(ps1))
    st = {d["udid"]: d.get("state") for d in devices()}
    sims = []
    for i, u in enumerate(udids):
        sj, err = res.get(u, (None, None))
        raw = ""
        if u in raws and os.path.exists(raws[u]):
            with open(raws[u]) as f:
                raw = f.read()
        sims.append({"udid": u, "role": roles[i], "simfps": sj, "simfps_error": err, "raw": raw, "injections": inj[u],
                     "actions": acts[u], "proc": per.get(u), "state_after": st.get(u),
                     "pace": read_pace(u) if mix != "idle" and roles[i] != "static" else []})
    m0, m1 = mem_summary(vm0), mem_summary(vm1)
    return {"t_go": t_go, "t_start": t_start, "t_end": t_end, "secs": round(t_end - t_start, 2), "sims": sims,
            "guest": dict(tot, mem=m1, mem_before=m0, swapins_d=m1["swapins"] - m0["swapins"], swapouts_d=m1["swapouts"] - m0["swapouts"],
                          pageouts_d=m1["pageouts"] - m0["pageouts"], pressure_before=lvl0,
                          pressure_max=max([x for x in lvls + [pressure_level()] if x] or [None]) if any(lvls) else pressure_level()),
            "new_ips": sorted(list_ips() - ips0), "procs_total": len(ps1)}


def cmd_measure(a):
    udids, roles = [u for u in a.udids.split(",") if u], [r for r in a.roles.split(",") if r]
    tmp = tempfile.mkdtemp(prefix="benchwin")
    try:
        t_go = a.start_at or now() + 3 + len(udids)
        out_json(run_window(a, udids, roles, a.mix, a.secs, t_go, tmp, a.probe_method, a.probe_every, a.probe_cmd, a.no_strip, a.probe_timeout))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_calibrate(a):
    """Which injection method is answered? 3 injections of each method on one simulator, watched by simfps."""
    from macbench import analysis
    tmp = tempfile.mkdtemp(prefix="benchcal")
    res = {}
    try:
        for method in a.methods.split(","):
            f = a.time_scale
            t_go = now() + 3 * f
            raw = os.path.join(tmp, "raw.txt")
            h = spawn_simfps(a.udid, 9 * f, "cal", False, t_go, raw)
            inj, state = [], {}
            for k in range(3):
                d = t_go + (1.5 + k * 2.4) * f - now()
                if d > 0:
                    time.sleep(d)
                t0 = now()
                rc = inject(method, a.udid, state, a.probe_cmd)
                inj.append([t0, now(), rc])
            sj, err = h.wait(40 + 9 * f)
            txt = open(raw).read() if os.path.exists(raw) else ""
            r = analysis.probe_latencies(analysis.parse_raw(txt), [(x[0], x[1]) for x in inj], 2.0)
            res[method] = {"answered": r["n"] - r["misses"], "n": r["n"], "p50_ms": r["p50_ms"], "call_p50_ms": r["call_p50_ms"],
                           "error": err}
        out_json({"methods": res})
    finally:
        simctl("ui", a.udid, "appearance", "light", timeout=30)          # the appearance method leaves it dark
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_shots(a):
    """K screenshots of each simulator: pixel hash, tear check, strip counter. PNGs stay here."""
    from macbench import png, strip, tears
    tmp = tempfile.mkdtemp(prefix="benchshots")
    res = []
    try:
        for u in a.udids.split(","):
            one = {"udid": u, "shots": []}
            for k in range(a.count):
                p = os.path.join(tmp, "%s-%d.png" % (u[:8], k))
                rc = simctl("io", u, "screenshot", "--type=png", p, timeout=60)[0]
                if rc != 0 or not os.path.exists(p):
                    one["shots"].append({"ok": False})
                    continue
                with open(p, "rb") as f:
                    img = png.read_png(f.read())
                t = tears.detect_tears(img)
                one["shots"].append({"ok": True, "w": img.w, "h": img.h, "hash": img.pixel_hash(),
                                     "counter": strip.read_counter_image(img), "tear": t["tear"], "tear_applicable": t["applicable"]})
                os.unlink(p)
                time.sleep(0.4)
            res.append(one)
        out_json({"sims": res})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_cycle(a):
    """One lifecycle of a simulator: create, boot, install, launch, first app frame, shut down, resume
    (boot again), first frame again, delete. Timings in seconds."""
    from macbench import analysis
    tmp = tempfile.mkdtemp(prefix="benchcycle")
    t = {}
    udid = None
    try:
        name = "%s-cycle" % a.prefix
        t0 = now()
        udid = simctl("create", name, a.type, a.runtime)[1].strip().splitlines()[-1]
        t["create_s"] = round(now() - t0, 2)
        for phase in ("cold", "resume"):
            if phase == "resume":
                t0 = now()
                simctl("shutdown", udid, timeout=120)
                t["shutdown_s"] = round(now() - t0, 2)
            t0 = now()
            simctl("boot", udid, timeout=300)
            simctl("bootstatus", udid, timeout=600)
            t["%s_boot_s" % phase] = round(now() - t0, 2)
            if phase == "cold":
                t0 = now()
                simctl("install", udid, os.path.join(HERE, "..", "GlassBench.app"), timeout=120)
                t["install_s"] = round(now() - t0, 2)
            raw = os.path.join(tmp, "raw-%s.txt" % phase)
            h = spawn_simfps(udid, 14, "cycle", True, now() + 2, raw)
            time.sleep(2.5)
            t_l = now()
            simctl("launch", udid, BUNDLE_ID, "-probe", timeout=60)
            t["%s_launch_call_s" % phase] = round(now() - t_l, 2)
            sj, err = h.wait(60)
            frames = analysis.parse_raw(open(raw).read()) if os.path.exists(raw) else []
            first = next((x[0] for x in frames if x[0] >= t_l and x[1] >= 0 and x[1] != 65535), None)
            t["%s_first_frame_s" % phase] = None if first is None else round(first - t_l, 2)
        # clone of the shut-down, installed device (what a pool does for a new lease), then its boot
        simctl("shutdown", udid, timeout=120)
        t0 = now()
        cl = simctl("clone", udid, "%s-clone" % name, timeout=300)[1].strip().splitlines()[-1]
        t["clone_s"] = round(now() - t0, 2)
        try:
            t0 = now()
            simctl("boot", cl, timeout=300)
            simctl("bootstatus", cl, timeout=600)
            t["clone_boot_s"] = round(now() - t0, 2)
        finally:
            simctl("shutdown", cl, timeout=120)
            simctl("delete", cl, timeout=120)
        t0 = now()
        simctl("delete", udid, timeout=120)
        t["delete_s"] = round(now() - t0, 2)
        udid = None
        out_json({"timings": t})
    finally:
        if udid:
            simctl("shutdown", udid, timeout=60)
            simctl("delete", udid, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_teardown(a):
    """Shut down and delete EVERY device in the bench set (it is the bench's own, marker file checked)."""
    simctl("shutdown", "all", timeout=300)
    simctl("delete", "all", timeout=300)
    left = [d["udid"] for d in devices()]
    booted = [d["udid"] for d in devices() if d.get("state") == "Booted"]
    out_json({"left": len(left), "booted": len(booted), "calls": [c[1:] for c in CALLS][-3:]})


def count_launchd_sim():
    """Simulators running on this Mac in any device set (one launchd_sim each): the pool's, a lease's, the bench's."""
    return len([p for p in parse_ps(ps_text()) if re.search(r"(^|/)launchd_sim( |$)", p["cmd"])])


def cmd_ips(a):
    out_json({"ips": sorted(list_ips())})


def cmd_memcheck(a):
    vm = mem_summary(parse_vm_stat(vmstat_text()))
    own = len([d for d in devices() if d.get("state") == "Booted"])
    out_json({"mem": vm, "pressure": pressure_level(), "booted": own, "foreign_booted": max(0, count_launchd_sim() - own),
              "ips": len(list_ips())})


def cmd_build(a):
    """Builds the bench tools in a scratch copy: cold (a module cache that does not exist yet), then warm (the same
    cache again); optionally an xcodebuild of a project given on the command line (cold: new DerivedData; warm: again)."""
    root = os.path.abspath(os.path.join(HERE, ".."))
    tmp = tempfile.mkdtemp(prefix="benchbuild")
    res = {}
    try:
        for f in ("simfps.swift", "build.sh"):
            shutil.copy(os.path.join(root, f), tmp)
        for d in ("GlassBench", "GenieBench"):
            shutil.copytree(os.path.join(root, d), os.path.join(tmp, d))
        mc = os.path.join(tmp, "modcache")
        env = dict(os.environ, CLANG_MODULE_CACHE_PATH=mc)
        for phase in ("cold", "warm"):
            rc, _, err, secs = run(["/bin/bash", os.path.join(tmp, "build.sh")], 1800, env=env)
            res["tools_%s_s" % phase] = round(secs, 1)
            res["tools_%s_rc" % phase] = rc
        if a.xcodebuild:
            dd = os.path.join(tmp, "dd")
            for phase in ("cold", "warm"):
                rc, _, err, secs = run(["xcodebuild"] + a.xcodebuild.split() + ["-derivedDataPath", dd], 3600)
                res["xcodebuild_%s_s" % phase] = round(secs, 1)
                res["xcodebuild_%s_rc" % phase] = rc
        out_json(res)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_calls(a):
    out_json({"calls": CALLS})


def main(argv=None):
    global SET
    if STUB:         # after every function is defined: the stub replaces the primitives (run, ps_text, list_ips...)
        from guest import benchguest_stub
        benchguest_stub.install(sys.modules[__name__])
    ap = argparse.ArgumentParser(prog="benchguest")
    ap.add_argument("--set", default=os.environ.get("BENCH_SET"))
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *opts):
        p = sp.add_parser(name)
        p.set_defaults(fn=fn)
        for o in opts:
            p.add_argument(*o[0], **o[1])
        return p
    O = lambda *n, **k: (n, k)
    add("info", cmd_info)
    add("init-set", cmd_init_set)
    add("ensure-type", cmd_ensure_type, O("--scale", default="1"), O("--allow-restart", action="store_true"))
    add("create", cmd_create, O("--n", type=int, required=True), O("--type", required=True), O("--runtime", required=True),
        O("--prefix", default="bench"), O("--start", type=int, default=0))
    add("slim", cmd_slim, O("--udids", required=True), O("--cmd", required=True))
    add("boot", cmd_boot, O("--udids", required=True), O("--parallel", action="store_true"))
    add("prepare", cmd_prepare, O("--udids", required=True), O("--roles", required=True), O("--install", action="store_true"),
        O("--quiesce", action="store_true"))
    add("settle", cmd_settle, O("--max-s", type=float, default=120), O("--bg-pct", type=float, default=40), O("--step", type=float, default=3))
    add("calibrate", cmd_calibrate, O("--udid", required=True), O("--methods", default="openurl,appearance"), O("--probe-cmd"), O("--time-scale", type=float, default=1.0))
    add("measure", cmd_measure, O("--udids", required=True), O("--roles", required=True), O("--mix", default="all-busy"),
        O("--secs", type=float, default=40), O("--start-at", type=float, default=0), O("--probe-method", default="openurl"),
        O("--probe-every", type=float, default=2.5), O("--probe-cmd"), O("--agent-period", type=float, default=12),
        O("--no-strip", action="store_true"), O("--probe-timeout", type=float, default=2.0))
    add("shots", cmd_shots, O("--udids", required=True), O("--count", type=int, default=2))
    add("cycle", cmd_cycle, O("--type", required=True), O("--runtime", required=True), O("--prefix", default="bench"))
    add("teardown", cmd_teardown)
    add("build", cmd_build, O("--xcodebuild", default=""))
    add("memcheck", cmd_memcheck)
    add("ips", cmd_ips)
    add("calls", cmd_calls)
    a = ap.parse_args(argv)
    SET = a.set
    try:
        if a.cmd != "init-set":
            if a.cmd not in ("info", "ensure-type"):
                check_set()
        a.fn(a)
    except GuestError as e:
        out_json({"error": str(e)})
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
