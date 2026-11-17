"""`macbox bench <subcommand>`: argument parsing and the wiring of a run."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import abrule, ab, chart, compare, core, fakes, hostprobe, others, scale, schema, targets
from .runctx import DeadlineError, Run
from .transport import GuestError, LocalGuest, SshGuest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def add_common(p):
    p.add_argument("--out", default=os.environ.get("BENCH_OUT", "bench-results"), help="directory for the result file")
    p.add_argument("--deadline", type=int, default=5400, help="hard deadline in seconds: cleanup runs and the tool exits (default 5400)")
    p.add_argument("--tag", action="append", default=[], help="key=value recorded in the result (for example mesa=26.2.4)")
    p.add_argument("--build-id", default=os.environ.get("BENCH_BUILD_ID"), help="the VM's build id (macbox bench fills it in)")
    p.add_argument("--min-guest-free-gib", type=float, default=1.5, help="refuse to boot more simulators below this guest free memory")
    p.add_argument("--min-host-free-gib", type=float, default=6.0, help="refuse to boot more simulators below this host available memory")
    p.add_argument("--disk-path", default=os.environ.get("BENCH_DISK_PATH") or os.environ.get("BENCH_HOME"),
                   help="a path on the filesystem that holds the VM's storage and that your storage guard watches (default $BENCH_DISK_PATH, else the macbox home); a reading that fails refuses to run")
    p.add_argument("--disk-start-gib", type=float, default=18.0, help="start a run only with this much free disk (GiB)")
    p.add_argument("--disk-boot-gib", type=float, default=14.0, help="boot another simulator only with this much free disk (GiB)")
    p.add_argument("--disk-abort-gib", type=float, default=11.0, help="below this the run is aborted and cleaned up while it waits (GiB)")
    p.add_argument("--max-compressor-mb", type=float, default=2048.0, help="refuse to boot (abort while waiting) when the guest's compressor holds more than this")
    p.add_argument("--watch-every", type=float, default=10.0, help=argparse.SUPPRESS)
    p.add_argument("--watch-guest-every", type=float, default=30.0, help=argparse.SUPPRESS)
    p.add_argument("--set-name", default=os.environ.get("BENCH_SET_NAME", "bench"), help="the bench's own device set in the guest (kept between runs: the seed lives in it)")
    p.add_argument("--allow-foreign", action="store_true", help="go on although simulators outside the bench are booted (recorded)")
    p.add_argument("--allow-restart", action="store_true", help="let a NEW device type restart CoreSimulatorService (only when nothing is booted)")
    p.add_argument("--scale-x", dest="scale_x", type=int, choices=(1, 2, 3), default=1, help="device pixel scale: 1 = the macbox 1x type, 3 = the stock iPhone (default 1)")
    p.add_argument("--secs", type=float, default=40, help="length of a measurement window, seconds")
    p.add_argument("--nominal-fps", type=float, default=60.0)
    p.add_argument("--probe-method", default="auto", choices=("auto", "notify", "appearance", "openurl", "cmd", "none"), help="input injection (auto: notify, else appearance, checked at the first N; openurl raises an alert on iOS 26 that stays over the app)")
    p.add_argument("--probe-cmd", default=None, help="for --probe-method cmd: a shell command with {udid}; it must flip GlassBench's marker (a tap)")
    p.add_argument("--probe-every", type=float, default=2.5, help="seconds between input probes of one simulator")
    p.add_argument("--probe-timeout", type=float, default=2.0, help="an input probe not answered in this time is a miss (counted at this value)")
    p.add_argument("--agent-period", type=float, default=12.0)
    p.add_argument("--settle-max", type=float, default=300, help="a settle (the guest quiet for two samples) that reaches this many seconds FAILS the run")
    p.add_argument("--lead", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--settle-step", type=float, default=10.0, help=argparse.SUPPRESS)
    p.add_argument("--settle-bg", type=float, default=40, help="the guest counts as settled below this background CPU %% (of one core)")
    p.add_argument("--stock-devices", action="store_true", help="create FRESH STOCK devices instead of cloning the production seed: about 1.8 GB of guest disk each, not what an agent gets; for a device type Simlock has no source for or a scenario that needs a stock device (recorded in the result)")
    p.add_argument("--rebuild-seed", action="store_true", help="delete the bench's seed and make a new one (after `macbox bench tools` rebuilt the app, or when it cannot be reused)")
    p.add_argument("--source-set", default=os.environ.get("BENCH_SIMLOCK_SOURCES"), help="Simlock's source device set in the guest, read only (default ~/.simlock/devices/ios-sources)")
    p.add_argument("--simslim", default=os.environ.get("BENCH_SIMSLIM"), help="simslim in the guest (default ~/go/bin/simslim)")
    p.add_argument("--slim-profile", default=os.environ.get("BENCH_SLIM_PROFILE"), help="simslim profile in the guest (default: the only .json in ~/.config/simslim)")
    p.add_argument("--seed-settle-max", type=float, default=600, help="the seed must settle within this many seconds or it is deleted and the run fails")
    p.add_argument("--quiesce", action="store_true", help="disable mediaanalysisd in each simulator (recorded; production does not)")
    p.add_argument("--strict-crashes", action="store_true", help="any new crash report on the Mac fails the run (default: only reports of the bench's programs and the simulator's graphics path; the others are listed)")
    p.add_argument("--crash-names", default=os.environ.get("BENCH_CRASH_NAMES", ""), help="more process names whose crash report fails the run, comma-separated")
    p.add_argument("--fail-on-swap", action="store_true", help="a guest swap-out in a window fails the memory gate (default: reported, host swap fails)")
    p.add_argument("--golden-file", default=os.path.join(BENCH_DIR, "baselines", "golden.json"))


def parser():
    ap = argparse.ArgumentParser(prog="macbox bench", description="scaling, regression and A/B suite for the macbox simulator stack (src/bench/README.md)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("scale", help="N = 1, 2, 3... simulators, per-simulator numbers, the maximum sustainable N per quality target")
    add_common(s)
    s.add_argument("--n", dest="ns", default="1,2,3,4,5,6", help="the N list, e.g. 1,2,3,4,6,8")
    s.add_argument("--mix", choices=("all-busy", "one-busy", "agent", "idle"), default="all-busy")
    s.add_argument("--targets", action="append", default=None, help="'name: fps>=55, p95<=25' (repeatable; default: smooth60, smooth30, responsive)")
    s.add_argument("--floor", default="fps>=20", help="stop the sweep when a simulator breaches this (default fps>=20)")
    s.add_argument("--shots", type=int, default=2, help="screenshots per simulator per step for the tear gate (0: none)")
    s.add_argument("--no-observer-check", dest="observer_check", action="store_false", help="skip the strip-read cost window at the first N")
    s.add_argument("--no-golden", dest="golden_phase", action="store_false", help="skip the static reference screen at the first N")
    for name, hlp in (("seed", "make the production seed (or report the one that is there)"), ("idle", "the VM with no simulators: the standing cost"), ("lifecycle", "create, boot, install, launch, shut down, resume, delete; leak per cycle; boot storm"),
                      ("soak", "N busy simulators for minutes: memory growth, frame rate drift"), ("build", "build timing: the bench tools, optionally an xcodebuild"),
                      ("panel", "the Device panel's stream (optional, needs Simlock/T3 Code)"), ("golden", "record the static screen's pixel hash for this setup"),
                      ("ab", "A/B one knob, A B B A inside one window, ADOPT or CLOSE")):
        p = sp.add_parser(name, help=hlp)
        add_common(p)
        if name == "idle":
            p.add_argument("--bar-qemu-cores", type=float, default=0.15, help="PASS bar for QEMU's CPU in cores")
        if name == "lifecycle":
            p.add_argument("--cycles", type=int, default=5, help="create/boot/.../delete cycles (the leak gates need at least 4)")
            p.add_argument("--storm", type=int, default=4, help="boot this many at once (0: skip)")
            p.add_argument("--storm-bar-s", type=float, default=300)
            p.add_argument("--leak-rss-mb", type=float, default=20, help="FAIL above this steady growth of QEMU's memory (RSS + swap) per cycle (MB)")
            p.add_argument("--leak-guest-mb", type=float, default=30, help="FAIL above this guest memory growth per cycle (MB)")
        if name == "soak":
            p.add_argument("--soak-n", type=int, default=3)
            p.add_argument("--soak-minutes", type=float, default=15)
            p.add_argument("--soak-every", type=float, default=30)
            p.add_argument("--soak-rss-mb-min", type=float, default=5, help="FAIL above this steady growth of QEMU's memory (RSS + swap) (MB/min)")
            p.add_argument("--soak-guest-mb-min", type=float, default=10, help="FAIL above this guest memory growth (MB/min)")
        if name == "build":
            p.add_argument("--xcodebuild", default="", help="arguments for xcodebuild (a project or workspace and scheme) to time cold and warm")
        if name == "panel":
            p.add_argument("--panel-cmd", default=None, help="command printing {\"frames\": [[t, counter], ...]}")
            p.add_argument("--panel-fps", type=float, default=55.0)
            p.add_argument("--panel-p95", type=float, default=25.0)
        if name == "ab":
            p.add_argument("--knob", help="live:KEY=VALUE (a line in the VM's live switch file), or use --set-cmd/--unset-cmd")
            p.add_argument("--set-cmd")
            p.add_argument("--unset-cmd")
            p.add_argument("--n", type=int, default=3, help="simulators")
            p.add_argument("--mix", choices=("all-busy", "one-busy", "agent"), default="all-busy")
            p.add_argument("--metric", choices=sorted(ab.METRICS), default="total_fps")
            p.add_argument("--tails", default=None, help="comma list of metrics that may not worsen (default per metric)")
            p.add_argument("--threshold", type=float, default=abrule.DEFAULT_THRESHOLD, help="effect needed to ADOPT, fixed BEFORE the run (default 0.04 = the run-to-run noise)")
            p.add_argument("--tail-tol", type=float, default=abrule.DEFAULT_THRESHOLD)
            p.add_argument("--arm-settle", type=float, default=10)
    c = sp.add_parser("compare", help="two result files")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--noise", type=float, default=compare.NOISE)
    h = sp.add_parser("chart", help="an HTML/SVG scaling chart from a scale result")
    h.add_argument("result")
    h.add_argument("-o", "--output")
    r = sp.add_parser("report", help="the PASS/FAIL lines of a result")
    r.add_argument("result")
    t = sp.add_parser("tools", help="copy the bench to the Mac and build simfps and GlassBench there")
    t.add_argument("--no-build", action="store_true")
    sp.add_parser("check", help="what this host and the VM offer the bench")
    cl = sp.add_parser("cleanup", help="shut down and delete what an earlier run left in the bench's device set (seeds stay unless --seeds)")
    cl.add_argument("--seeds", action="store_true", help="delete the seeds too (the next run makes a new one)")
    cl.add_argument("--set-name", default=os.environ.get("BENCH_SET_NAME", "bench"))
    return ap


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def qemu_pid_from_docker():
    cont = os.environ.get("MACBOX_CONTAINER", "macbox")
    try:
        out = subprocess.run([os.environ.get("MACBOX_DOCKER", "docker"), "top", cont, "-eo", "pid,args"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if "qemu-system-x86_64" in line:
            return int(line.split()[0])
    return None


class Golden:
    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                self.d = json.load(f)
        except (OSError, ValueError):
            self.d = {}

    def key(self, ctx, w, h):
        g = ctx.doc["env"].get("guest", {})
        return "%s|runtime %s|%s|%dx%d" % (g.get("macos"), g.get("runtime_used"), g.get("device_type"), w, h)

    def record(self, key, h):
        self.d[key] = h
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.d, f, indent=1, sort_keys=True)
            f.write("\n")


def build_ctx(a, kind):
    dry = os.environ.get("DRY") == "1"
    run = Run(a.deadline, log, margin=float(os.environ.get("BENCH_DEADLINE_MARGIN", "60")))
    alias = os.environ.get("MACBOX_SSH_HOST", "")
    clean = schema.scrubber([alias] if alias and alias != "macbox" else [])      # not user names or the default alias: ordinary words
    stamp = schema.stamp()
    set_name = a.set_name                # ONE set for every run: the seed made in it is kept and reused
    faults = os.environ.get("BENCH_STUB_FAULT", "")
    if dry:
        tmp = os.environ.get("BENCH_DRY_KEEP") or tempfile.mkdtemp(prefix="macbench-dry-")
        os.makedirs(tmp, exist_ok=True)
        if not os.environ.get("BENCH_DRY_KEEP"):
            run.teardown.insert(0, ("dry state directory", lambda: shutil.rmtree(tmp, ignore_errors=True)))
        a.lead, a.settle_step, a.watch_every, a.watch_guest_every = 1.0, 0.3, 0.5, 1.0
        host = fakes.FakeHost(faults)
        coh = fakes.FakeCoherent(host, faults)
        guest = LocalGuest(run, set_name, os.path.join(tmp, "guest"), BENCH_DIR, faults)
        guest.fault_ssh_timeout = int(os.environ.get("BENCH_STUB_SSH_TIMEOUT", "0")) or None
        a.build_id = a.build_id or "dry-run"
        live = os.path.join(tmp, "live.conf")
        with open(live, "w") as f:
            f.write("surface_coherent=1\n")
        a.disk_path = tmp
    else:
        pid = int(os.environ.get("BENCH_QEMU_PID") or qemu_pid_from_docker() or 0)
        if not pid:
            raise SystemExit("macbox bench: no running QEMU (macbox up first)")
        runp = os.environ.get("BENCH_RUN") or ""
        live = os.path.join(runp, "live.conf") if runp and os.path.exists(os.path.join(runp, "live.conf")) else None
        host = hostprobe.Host(pid, live_conf=live)
        from .gates import Coherent
        census = hostprobe.qemu_env_value(pid, "REIMS_VGPU_CENSUS")          # the path QEMU was given, if it exists here too
        if not (census and os.path.isdir(census)):
            census = os.path.join(runp, "coherent") if runp else None
        coh = Coherent(census, os.environ.get("BENCH_QEMU_LOG"),
                       os.path.join(os.environ.get("BENCH_HOME", ""), "home/.cache/m2v-gpu/hang") if os.environ.get("BENCH_HOME") else None)
        guest = SshGuest(run, os.environ.get("BENCH_SSH") or os.environ.get("MACBOX_SSH_HOST") or "macbox", set_name)
    os.makedirs(a.out, exist_ok=True)
    params = {k: v for k, v in vars(a).items() if k not in ("cmd", "out", "golden_file", "disk_path", "tag") and not callable(v)}
    doc = schema.new_result(kind if kind in schema.KINDS else "scale", params, {"host": {}, "guest": {}}, stamp)
    if kind == "scale":
        doc["data"] = {"mix": a.mix, "scale_x": a.scale_x, "steps": [], "summary": {"max_n": {}}}
    path = os.path.join(a.out, "%s-%s%s.json" % (kind, stamp, "-dry" if dry else ""))
    ctx = core.Ctx(a, run, guest, host, coh, doc, path, clean, log)
    ctx.live_conf = live
    gold = Golden(a.golden_file)
    ctx.golden_store = gold
    ctx.golden = lambda w, h: (gold.d.get(gold.key(ctx, w, h)), gold.key(ctx, w, h))
    ctx.golden_key_for = lambda w, h: gold.key(ctx, w, h)
    ctx.record_golden = gold.record
    if dry:
        ctx.dry_dir = tmp
    return ctx


def execute(a, kind, body):
    ctx = build_ctx(a, kind)
    run = ctx.run
    run.install()
    run.on_abort = lambda reason: ctx.section("abort", "FAIL", "ABORT: %s" % reason)
    run.final = lambda: finalize(ctx)
    core.monitor(ctx)
    if ctx.host.dry:
        log("DRY=1: stub guest, fake host; nothing real is touched")
    rc = 0
    try:
        try:
            body(ctx)
        finally:
            if not run.aborted:          # a monitor that ended the run owns the result: no one else writes into it
                core.run_gates(ctx)      # every scenario: Coherent, kernel GPU lines and crash reports since the run began
    except core.Stop as e:
        if not run.aborted:
            ctx.section("stopped", "FAIL", "STOPPED: %s" % e)
        rc = 2
    except (GuestError, DeadlineError) as e:
        if not run.aborted:
            ctx.section("error", "FAIL", "ERROR: %s: %s" % (type(e).__name__, e))
        rc = 2
    finally:
        if not run.aborted:
            run.cleanup()
        run.join_abort()                 # when a monitor aborted the run it writes the result and owns the exit code
        rc = max(rc, finalize(ctx))
    fails = [s for s in ctx.doc["sections"] if s["status"] == "FAIL"]
    if fails:
        return max(rc, 1)
    return rc


CLEANUP_FAILED_RC = 20


def finalize(ctx):
    """After the cleanup: its section (PASS only when verified), the idle guard's, the result file. Also what the abort
    path of the run's monitor calls. Returns CLEANUP_FAILED_RC when the cleanup could not delete everything."""
    run, info = ctx.run, ctx.cleanup_info
    rc = 0
    if ctx.taken or info or run.problems:
        left_ok = bool(info) and not info.get("failed") and info.get("booted", 0) == 0 and info.get("left", 0) == 0
        sec = "PASS" if left_ok and not run.problems else "FAIL"
        if sec == "FAIL":
            rc = CLEANUP_FAILED_RC
        ctx.section("cleanup", sec, "CLEANUP %s: bench simulators left booted %s, devices left %s, knobs restored %s%s" % (
            sec, info.get("booted", "n/a"), info.get("left", "n/a"), "yes" if not run.problems else "NO",
            ("; " + "; ".join(run.problems)) if run.problems else ""))
    if ctx.idle_guard:
        absent = "absent" in ctx.idle_guard
        ctx.doc["env"].setdefault("guest", {})["idle_guard"] = "absent" if absent else "kept"
        ctx.section("idle_guard", "INFO", ("IDLE GUARD ABSENT: sim-idle-guard is not installed on the Mac, nothing keeps the simulators from an idle shutdown; "
                                           "the run counts its booted simulators before every window") if absent else "IDLE GUARD kept every bench simulator for the run")
    problems = ctx.save()
    print("result: %s%s" % (os.path.basename(ctx.out_path), "" if not problems else "  (NOT publishable as is: %s)" % "; ".join(problems[:3])))
    return rc


def main(argv=None):
    a = parser().parse_args(argv)
    c = a.cmd
    if c in ("compare", "report", "chart"):
        return offline(a)
    if c == "tools":
        return tools(a)
    if c == "check":
        return check(a)
    if c == "cleanup":
        return cleanup(a)
    if c == "scale":
        a.ns = [int(x) for x in a.ns.split(",")]
        a.targets = a.targets or targets.DEFAULT_TARGETS
        for t in a.targets + ["floor: " + a.floor]:
            targets.parse_target(t)
        return execute(a, "scale", scale.run_scale)
    body = {"seed": others.run_seed, "idle": others.run_idle, "lifecycle": others.run_lifecycle, "soak": others.run_soak, "build": others.run_build,
            "panel": others.run_panel, "golden": others.run_golden, "ab": ab.run_ab}[c]
    for k, v in (("ns", [1]), ("mix", "all-busy"), ("targets", targets.DEFAULT_TARGETS), ("floor", "fps>=20"), ("shots", 2),
                 ("observer_check", False), ("golden_phase", False)):
        if not hasattr(a, k):
            setattr(a, k, v)
    return execute(a, {"golden": "gates"}.get(c, c), body)


def cleanup(a):
    """macbox bench cleanup: shut down and delete what an earlier run left in the bench's set; exit 0 only when verified."""
    run = Run(900, log)
    faults = os.environ.get("BENCH_STUB_FAULT", "")
    if os.environ.get("DRY") == "1":
        keep = os.environ.get("BENCH_DRY_KEEP")
        if not keep:
            raise SystemExit("DRY cleanup needs BENCH_DRY_KEEP (the stub guest's state directory)")
        guest = LocalGuest(run, a.set_name, os.path.join(keep, "guest"), BENCH_DIR, faults)
    else:
        guest = SshGuest(run, os.environ.get("BENCH_SSH") or os.environ.get("MACBOX_SSH_HOST") or "macbox", a.set_name)
    try:
        guest.call(["init-set"], 60)
        info = guest.call(["teardown"] + (["--seeds"] if a.seeds else []), 300)
    except (GuestError, DeadlineError) as e:
        print("CLEANUP FAIL: the guest could not be reached or refused: %s" % e)
        return CLEANUP_FAILED_RC
    ok = not (info.get("failed") or info.get("left") or info.get("booted"))
    print("CLEANUP %s: devices left %s, booted %s, seeds kept %s%s" % ("PASS" if ok else "FAIL", info.get("left"), info.get("booted"), info.get("seeds"),
                                                                       ("; could not: " + "; ".join(info["failed"])) if info.get("failed") else ""))
    return 0 if ok else CLEANUP_FAILED_RC


def offline(a):
    if a.cmd == "compare":
        text, worse = compare.compare(schema.load(a.a), schema.load(a.b), a.noise)
        print(text)
        return 1 if worse else 0
    doc = schema.load(a.result)
    if a.cmd == "report":
        print("%s %s %s (build %s)" % (doc["kind"], doc["id"], doc["created"], doc["env"].get("build_id")))
        for s in doc["sections"]:
            print("%-4s %s" % (s["status"], s["line"]))
        return 1 if any(s["status"] == "FAIL" for s in doc["sections"]) else 0
    if doc["kind"] != "scale":
        print("chart: only scale results have a scaling chart", file=sys.stderr)
        return 2
    out = a.output or os.path.splitext(a.result)[0] + ".html"
    with open(out, "w") as f:
        f.write(chart.scale_chart(doc))
    print(out)
    return 0


def tools(a):
    ssh = os.environ.get("BENCH_SSH") or os.environ.get("MACBOX_SSH_HOST") or "macbox"
    if os.environ.get("DRY") == "1":
        print("DRY=1: would copy %s to the guest's ~/.macbox/bench and run build.sh" % BENCH_DIR)
        return 0
    tar = subprocess.Popen(["tar", "-C", BENCH_DIR, "--exclude=__pycache__", "--exclude=tests", "--exclude=baselines", "-cf", "-", "."], stdout=subprocess.PIPE)
    r = subprocess.run(["ssh", ssh, "mkdir -p ~/.macbox/bench && tar -C ~/.macbox/bench -xf -"], stdin=tar.stdout)
    tar.wait()
    if r.returncode:
        return 2
    if a.no_build:
        return 0
    return subprocess.run(["ssh", ssh, "cd ~/.macbox/bench && ./build.sh"]).returncode


def check(a):
    pid = int(os.environ.get("BENCH_QEMU_PID") or qemu_pid_from_docker() or 0)
    ok = True

    def line(good, text):
        nonlocal ok
        ok &= good
        print("%s %s" % ("ok  " if good else "MISS", text))
    line(bool(pid), "QEMU running (pid known)")
    if pid:
        hp = hostprobe.Host(pid, live_conf=os.path.join(os.environ.get("BENCH_RUN", ""), "live.conf"))
        s = hp.snap()
        line(bool(s["threads"]), "QEMU thread CPU readable (%d threads)" % len(s["threads"]))
        line(s["gpu"] is not None, "GPU render time readable from DRM fdinfo (i915 drm-engine-render)")
        line(s["mem"].get("cg_current") is not None, "the VM's cgroup memory readable")
        n, note = hostprobe.kernel_gpu_lines(time.time() - 60)
        line(n is not None, "kernel journal readable for GPU error lines" + ("" if n is not None else " (%s)" % note))
        line(hp.mem_avail_gb() is not None, "host available memory readable")
    ssh = os.environ.get("BENCH_SSH") or os.environ.get("MACBOX_SSH_HOST") or "macbox"
    try:
        src = os.environ.get("BENCH_SIMLOCK_SOURCES")
        out = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", ssh, "cd ~/.macbox/bench && %sxcrun python3 guest/benchguest.py info" % (
                              "BENCH_SIMLOCK_SOURCES=%s " % shlex.quote(src) if src else "")],
                             capture_output=True, text=True, timeout=60).stdout
        d = json.loads(out.strip().splitlines()[-1])
        line(True, "guest answers: macOS %s, %s" % (d.get("macos"), d.get("xcode")))
        line(d.get("has_simfps"), "simfps built in the guest (macbox bench tools)")
        line(d.get("has_app"), "GlassBench.app built in the guest (macbox bench tools)")
        types = d.get("simlock_types") or []
        line(bool(types), "Simlock's source devices found (production simulators: %s)%s" % (", ".join(types) or "none",
                                                                                            "" if types else "; without them only --stock-devices works"))
        line(d.get("has_simslim") and d.get("has_slim_profile"), "simslim and its profile in the guest (the seed is made Slim with them)")
        line(d.get("has_idle_guard"), "sim-idle-guard in the guest (keeps the bench's simulators from its 15-minute shutdown)")
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        line(False, "the guest's bench helper (macbox bench tools)")
    return 0 if ok else 1
