"""The guest's stand-in for DRY=1 and the tests: a fake simctl, ps, vm_stat and simfps that keep their state in a
directory ($BENCHGUEST_STATE) between calls, so `benchguest.py` runs its real control flow with no macOS.
Faults ($BENCH_STUB_FAULT, comma separated): memfloor@N (guest free memory below any floor from N booted
simulators), crash@N (a new crash report at N booted), backwards@N (a frame shown out of order at N), slow@N
(frame rate collapses from N), hang_simfps (simfps never answers), nomarker (the input marker never flips)."""
import json
import os
import random
import sys
import time
import uuid

STATE = os.environ.get("BENCHGUEST_STATE", "/tmp/benchguest-stub")
FAULTS = [f for f in os.environ.get("BENCH_STUB_FAULT", "").split(",") if f]
T0 = float(os.environ.get("BENCH_STUB_T0", "0")) or time.time()


def _p(name):
    return os.path.join(STATE, name)


def load():
    try:
        with open(_p("state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"devs": {}, "inj": [], "mode": {}, "ips": [], "pid": 1000}


def save(s):
    os.makedirs(STATE, exist_ok=True)
    with open(_p("state.json.tmp"), "w") as f:
        json.dump(s, f)
    os.replace(_p("state.json.tmp"), _p("state.json"))


def fault(name):
    for f in FAULTS:
        if f == name or f.startswith(name + "@"):
            return int(f.split("@")[1]) if "@" in f else 0
    return None


def booted(s):
    return [u for u, d in s["devs"].items() if d["state"] == "Booted"]


def _log(argv):
    os.makedirs(STATE, exist_ok=True)
    with open(os.environ.get("BENCHGUEST_CALLS", _p("calls.log")), "a") as f:
        f.write(json.dumps(argv) + "\n")


RUNTIMES = {"runtimes": [{"identifier": "stub.runtime.iOS-26-5", "name": "iOS 26.5", "version": "26.5", "buildversion": "stub",
                          "platform": "iOS", "isAvailable": True,
                          "supportedDeviceTypes": [{"name": "iPhone 17 Pro", "identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                                                    "bundlePath": "/nonexistent/iPhone 17 Pro.simdevicetype"}]}]}
TYPES = {"devicetypes": [{"identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro", "name": "iPhone 17 Pro"},
                         {"identifier": "dev.macbox.CoreSimulator.SimDeviceType.iPhone-17-Pro-1x", "name": "iPhone 17 Pro (1x)"}]}


class _Lock:
    """One stub process at a time through the state file (threads and processes alike)."""
    def __enter__(self):
        import fcntl
        os.makedirs(STATE, exist_ok=True)
        self.f = open(_p("lock"), "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)

    def __exit__(self, *a):
        self.f.close()


def stub_run(argv, timeout=120, env=None):
    with _Lock():
        return _stub_run(argv)


def _stub_run(argv):
    _log(argv)
    s = load()
    out, rc = "", 0
    if argv[:3] == ["xcrun", "simctl", "--set"]:
        cmd = argv[4:]
        verb = cmd[0]
        if verb == "list":
            if cmd[1] == "devices":
                devs = [{"udid": u, "name": d["name"], "state": d["state"], "isAvailable": True} for u, d in s["devs"].items()]
                out = json.dumps({"devices": {"stub.runtime.iOS-26-5": devs}})
            else:
                out = json.dumps(RUNTIMES if cmd[1] == "runtimes" else TYPES)
        elif verb == "create":
            u = str(uuid.uuid4()).upper()
            s["devs"][u] = {"name": cmd[1], "state": "Shutdown", "type": cmd[2]}
            out = u + "\n"
        elif verb == "clone":
            u = str(uuid.uuid4()).upper()
            s["devs"][u] = {"name": cmd[2], "state": "Shutdown", "type": s["devs"][cmd[1]].get("type")}
            out = u + "\n"
        elif verb == "boot":
            time.sleep(0.05)
            s["devs"][cmd[1]]["state"] = "Booted"
        elif verb == "shutdown":
            for u in (list(s["devs"]) if cmd[1] == "all" else [cmd[1]]):
                if u in s["devs"]:
                    s["devs"][u]["state"] = "Shutdown"
        elif verb == "delete":
            for u in (list(s["devs"]) if cmd[1] == "all" else [cmd[1]]):
                s["devs"].pop(u, None)
        elif verb == "launch":
            s["mode"][cmd[1]] = "static" if "-static" in cmd else "busy"
        elif verb == "terminate":
            s["mode"].pop(cmd[1], None)
        elif verb in ("openurl", "ui"):
            if fault("nomarker") is None:
                s["inj"].append([time.time(), cmd[1]])
        elif verb == "io":
            p = cmd[-1]
            from macbench import png
            rows = [bytes([(x * 3 + y) % 251 for x in range(402) for _ in range(3)][:402 * 3]) for y in range(874)]
            with open(p, "wb") as f:
                f.write(png.write_png(rows, 402, 874, 3))
        elif verb == "get_app_container":
            d = _p("container")
            os.makedirs(os.path.join(d, "Documents"), exist_ok=True)
            u = cmd[1]
            now = time.time()
            with open(os.path.join(d, "Documents", "pace.json"), "w") as f:
                for k in range(8):
                    f.write(json.dumps({"at": now - 40 + 5 * k, "counter": int((now - 40 + 5 * k - T0) * 60) % 65536, "fps": 60.0, "ticks": 300}) + "\n")
            out = d + "\n"
    elif argv[:3] == ["xcrun", "simctl", "list"]:
        out = json.dumps(RUNTIMES if argv[3] == "runtimes" else TYPES) if argv[3] in ("runtimes", "devicetypes") else ""
    elif argv[0] in ("sw_vers",):
        out = "ProductName: macOS\nProductVersion: 26.0\n"
    elif argv[0] == "xcodebuild":
        out = "Xcode 26.0\nBuild version stub\n"
    elif argv[0] == "uname":
        out = "x86_64\n"
    else:
        out = ""
    save(s)
    return rc, out, "", 0.01


def stub_ps():
    s = load()
    now = time.time()
    lines = ["    1     0  4000 %s /sbin/launchd" % _cpu(now - T0, 0.05), "  100     1  60000 %s /usr/sbin/WindowServer" % _cpu(now - T0, 0.2)]
    pid = 200
    for i, u in enumerate(sorted(booted(s))):
        busy = s["mode"].get(u) == "busy"
        base = "/stub/set/%s/data" % u
        lines.append("%5d     1  20000 %s launchd_sim %s" % (pid, _cpu(now - T0, 0.05), base))
        lines.append("%5d %5d 150000 %s SpringBoard" % (pid + 1, pid, _cpu(now - T0, 0.15 if busy else 0.03)))
        lines.append("%5d %5d 300000 %s GlassBench" % (pid + 2, pid, _cpu(now - T0, 0.5 if busy else 0.01)))
        lines.append("%5d     1  90000 %s SimMetalHost -d %s" % (pid + 3, _cpu(now - T0, 0.3 if busy else 0.02), u))
        pid += 10
    return "\n".join(lines) + "\n"


def _cpu(elapsed, frac):
    secs = max(0.0, elapsed) * frac
    return "%d:%05.2f" % (int(secs // 60), secs % 60)


def stub_vmstat():
    s = load()
    n = len(booted(s))
    page = 16384
    total_pages = 16 * 1024 * 1024 * 1024 // page
    used = (3000 + 1100 * n) * 1048576 // page
    free = max(0, total_pages - used)
    mf = fault("memfloor")
    if mf is not None and n >= mf:
        free = 20 * 1048576 // page
    return ("Mach Virtual Memory Statistics: (page size of %d bytes)\nPages free:  %d.\nPages active:  %d.\nPages inactive:  1000.\n"
            "Pages speculative:  500.\nPages wired down:  %d.\nPages occupied by compressor:  %d.\nPageins:  1000.\nPageouts:  0.\n"
            "Swapins:  0.\nSwapouts:  0.\n" % (page, free, used // 2, used // 4, 5000 + 300 * n))


class StubSimfps:
    def __init__(self, udid, secs, strip, start_at, raw_path):
        self.udid, self.secs, self.strip, self.start_at, self.raw = udid, secs, strip, start_at, raw_path

    def wait(self, timeout):
        if fault("hang_simfps") is not None:
            return None, "simfps hung (killed after %ds)" % int(timeout)
        d = self.start_at + self.secs - time.time()
        if d > 0:
            time.sleep(d)
        s = load()
        n = len(booted(s))
        mode = s["mode"].get(self.udid, "busy")
        rng = random.Random(hash(self.udid) & 0xffff)
        fps = 60.0 if n <= 2 else max(8.0, 60.0 - 7.0 * (n - 2))
        sl = fault("slow")
        if sl is not None and n >= sl:
            fps = 14.0
        inj = sorted(t for t, u in s["inj"] if u == self.udid and self.start_at - 1 <= t <= self.start_at + self.secs)
        lines, t, c, lum, k = [], self.start_at, 100, 255, 0
        back = fault("backwards")
        lines.append("%.6f %d %d" % (t, c if self.strip else -1, lum))
        end = self.start_at + self.secs
        delay = [0.06 + rng.random() * 0.05 for _ in inj]
        while t < end:
            if mode == "busy":
                t += (1.0 / fps) * (0.9 + 0.2 * rng.random())
                c = (c + 1) % 65536
                if back is not None and n >= back and k == 50:
                    c = (c - 3) % 65536
                k += 1
            else:
                t += 0.05
            while inj and t >= inj[0] + delay[0]:
                lum = 0 if lum == 255 else 255
                inj.pop(0)
                delay.pop(0)
                if mode != "busy":
                    lines.append("%.6f %d %d" % (t, -1, lum))
            if mode == "busy":
                lines.append("%.6f %d %d" % (t, c if self.strip else -1, lum))
        with open(self.raw, "w") as f:
            f.write("\n".join(lines) + "\n")
        n_frames = len(lines)
        return {"fps": round(n_frames / self.secs, 1), "frames": n_frames, "secs": self.secs, "udid": self.udid}, None

    def kill(self):
        pass


def install(mod):
    mod.run = stub_run
    mod.ps_text = stub_ps
    mod.vmstat_text = stub_vmstat
    mod.sysctl = lambda name: {"kern.memorystatus_vm_pressure_level": "1", "hw.ncpu": "8", "hw.memsize": str(16 << 30)}.get(name, "")
    mod.spawn_simfps = lambda udid, secs, label, strip, start_at, raw: StubSimfps(udid, secs, strip, start_at, raw)

    calls = [0]

    def ips():
        # a crash report appears during a measurement (the second look of a window), not before it
        s = load()
        calls[0] += 1
        cr = fault("crash")
        if cr is not None and len(booted(s)) >= cr and calls[0] >= 2 and "GlassBench-stub.ips" not in s["ips"]:
            s["ips"].append("GlassBench-stub.ips")          # stays on the "disk": the run-wide gate sees it later
            save(s)
        return set(s["ips"])
    mod.list_ips = ips
