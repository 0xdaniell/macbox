"""What the Linux host says about the VM: QEMU's threads (user / guest / sys CPU), the GPU's render time from
DRM fdinfo, the VM's cgroup (memory, swap, pressure), host memory and swap, and kernel GPU error lines.
Passive reads of /proc and /sys only; no tracer, no GPU work. `root` lets the tests point at a fake tree."""
import os
import re
import subprocess

GPU_ERRORS = re.compile(
    r"fence expir|gpu hang|context reset|i915.*\bhang\b"
    r"|xe \S+: .*(timedout job|wedged|engine reset|gt\d+: (trying reset|reset (queued|started|failed))"
    r"|\*error\* (tile\d+: )?gt\d|fault response|faulted address|banned|coredump has been created)", re.I)


def _read(root, path):
    try:
        with open(root + path) as f:
            return f.read()
    except OSError:
        return None


def parse_task_stat(text):
    """/proc/<pid>/task/<tid>/stat -> (comm, utime, stime, guest_time) in clock ticks. comm may hold spaces and ')'."""
    i, j = text.index("("), text.rindex(")")
    comm = text[i + 1:j]
    f = text[j + 2:].split()          # f[0] is field 3 (state)
    return comm, int(f[11]), int(f[12]), int(f[40]) if len(f) > 40 else 0


def thread_class(comm):
    c = re.sub(r"\d+", "N", comm)
    return "vCPU" if c.startswith("CPU N/KVM") else c


def thread_snapshot(pid, root=""):
    out = {}
    base = "%s/proc/%d/task" % (root, pid)
    try:
        tids = os.listdir(base)
    except OSError:
        return out
    for tid in tids:
        t = _read(root, "/proc/%d/task/%s/stat" % (pid, tid))
        if t:
            try:
                out[tid] = parse_task_stat(t)
            except (ValueError, IndexError):
                pass
    return out


def thread_delta(a, b, secs, hz=100, top=6):
    """CPU of the process between two snapshots, in cores: user (host-side), guest (inside the VM), sys, total, and the
    top thread classes. guest_time is part of utime in Linux, so user = utime - guest."""
    cls, tot_u = {}, [0, 0, 0]
    for tid, (comm, ut, st, gt) in b.items():
        if tid not in a:
            continue
        du, ds, dg = ut - a[tid][1], st - a[tid][2], gt - a[tid][3]
        tot_u[0] += du - dg
        tot_u[1] += dg
        tot_u[2] += ds
        k = thread_class(comm)
        e = cls.setdefault(k, [0, 0.0])
        e[0] += 1
        e[1] += (du + ds) / hz / secs
    f = lambda x: round(x / hz / secs, 3)
    rows = sorted(cls.items(), key=lambda kv: -kv[1][1])[:top]
    return {"user": f(tot_u[0]), "guest": f(tot_u[1]), "sys": f(tot_u[2]), "cores": f(sum(tot_u)),
            "top": [{"name": k, "threads": n, "cores": round(c, 3)} for k, (n, c) in rows]}


def parse_fdinfo(text):
    """-> (client id, render ns or None). i915 states `drm-engine-render: <ns> ns`; xe states cycles (not converted:
    None, so a GPU number is never made up)."""
    cid = re.search(r"^drm-client-id:\s*(\S+)", text, re.M)
    r = re.search(r"^drm-engine-render:\s*(\d+)", text, re.M)
    if not cid:
        return None, None
    return cid.group(1), int(r.group(1)) if r else None


def gpu_render_ns(pid, root=""):
    """Sum of the render-engine busy ns over the process's distinct DRM clients; None when none is readable."""
    seen, got = {}, False
    base = "%s/proc/%d/fdinfo" % (root, pid)
    try:
        fds = os.listdir(base)
    except OSError:
        return None
    for fd in fds:
        t = _read(root, "/proc/%d/fdinfo/%s" % (pid, fd))
        if t and "drm-client-id" in t:
            cid, ns = parse_fdinfo(t)
            if ns is not None:
                seen[cid] = ns
                got = True
    return sum(seen.values()) if got else None


def parse_kv(text):
    d = {}
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) == 2 and p[1].lstrip("-").isdigit():
            d[p[0]] = int(p[1])
    return d


def parse_psi(text):
    """/proc/pressure/memory or memory.pressure -> {"some": {avg10,total}, "full": {...}}"""
    d = {}
    for line in (text or "").splitlines():
        p = line.split()
        if p and p[0] in ("some", "full"):
            d[p[0]] = {k: float(v) for k, v in (x.split("=") for x in p[1:])}
    return d


def cgroup_dir(pid, root=""):
    t = _read(root, "/proc/%d/cgroup" % pid)
    for line in (t or "").splitlines():
        if line.startswith("0::"):
            return "/sys/fs/cgroup" + line[3:]
    return None


def mem_snapshot(pid, root=""):
    """Host memory and swap counters, and the VM's cgroup's: every number is a counter or a level, deltas are taken by the caller."""
    mi = parse_kv((_read(root, "/proc/meminfo") or "").replace(":", " "))
    vs = parse_kv(_read(root, "/proc/vmstat"))
    cg = cgroup_dir(pid, root)
    s = {"mem_total_kb": mi.get("MemTotal"), "mem_avail_kb": mi.get("MemAvailable"), "swap_total_kb": mi.get("SwapTotal"),
         "swap_free_kb": mi.get("SwapFree"), "pswpin": vs.get("pswpin"), "pswpout": vs.get("pswpout"),
         "psi_some_us": parse_psi(_read(root, "/proc/pressure/memory")).get("some", {}).get("total")}
    if cg:
        for name, key in (("memory.current", "cg_current"), ("memory.swap.current", "cg_swap")):
            t = _read(root, cg + "/" + name)
            if t and t.strip().isdigit():
                s[key] = int(t.strip())
        ev = parse_kv(_read(root, cg + "/memory.events"))
        s["cg_high"], s["cg_max"], s["cg_oom"] = ev.get("high"), ev.get("max"), ev.get("oom_kill")
        s["cg_psi_some_us"] = parse_psi(_read(root, cg + "/memory.pressure")).get("some", {}).get("total")
    st = _read(root, "/proc/%d/status" % pid) or ""
    m = re.search(r"VmRSS:\s+(\d+)", st)
    s["qemu_rss_kb"] = int(m.group(1)) if m else None
    return s


def mem_delta(a, b, secs):
    d = lambda k: None if a.get(k) is None or b.get(k) is None else b[k] - a[k]
    out = {"mem_avail_gb": None if b.get("mem_avail_kb") is None else round(b["mem_avail_kb"] / 1048576.0, 2),
           "qemu_rss_mb": None if b.get("qemu_rss_kb") is None else round(b["qemu_rss_kb"] / 1024.0, 1),
           "pswpin_pages": d("pswpin"), "pswpout_pages": d("pswpout"),
           "cgroup_swap_mb": None if b.get("cg_swap") is None else round(b["cg_swap"] / 1048576.0, 1),
           "cgroup_swap_delta_mb": None if d("cg_swap") is None else round(d("cg_swap") / 1048576.0, 2),
           "cgroup_mem_gb": None if b.get("cg_current") is None else round(b["cg_current"] / 1073741824.0, 2),
           "cgroup_events": {k: d(k) for k in ("cg_high", "cg_max", "cg_oom") if d(k) is not None},
           "psi_mem_some_pct": None if d("psi_some_us") is None or secs <= 0 else round(100.0 * d("psi_some_us") / 1e6 / secs, 2),
           "cgroup_psi_some_pct": None if d("cg_psi_some_us") is None or secs <= 0 else round(100.0 * d("cg_psi_some_us") / 1e6 / secs, 2)}
    return out


def kernel_gpu_lines(since_epoch):
    """Kernel GPU error lines since a time: (count or None, note). None = the journal can't be read (not a PASS)."""
    try:
        p = subprocess.run(["journalctl", "-k", "--since", "@%d" % int(since_epoch), "--no-pager", "-q"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "journalctl unavailable: %s" % e
    if p.returncode != 0:
        return None, "journalctl: " + (p.stderr.strip()[:100] or "failed")
    lines = [l for l in p.stdout.splitlines() if GPU_ERRORS.search(l)]
    return len(lines), ("; ".join(l[-80:] for l in lines[-2:]) if lines else "")


def read_live_conf(path):
    """live.conf as {key: value} (read only)."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def qemu_env_switches(pid, root=""):
    """REIMS_VGPU_* names and values of the QEMU process (a directory value is shown as <dir>)."""
    t = _read(root, "/proc/%d/environ" % pid) or ""
    out = {}
    for kv in t.split("\0"):
        if kv.startswith("REIMS_VGPU_") and "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = "<dir>" if v.startswith("/") else v
    return out


def host_versions():
    kernel = os.uname().release
    mesa = None
    for argv in (["dpkg-query", "-W", "-f=${Version}", "mesa-vulkan-drivers"], ["rpm", "-q", "--qf", "%{VERSION}", "mesa-vulkan-drivers"]):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=10)
            if p.returncode == 0 and p.stdout.strip():
                mesa = p.stdout.strip()
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
    cpu = None
    try:
        with open("/proc/cpuinfo") as f:
            m = re.search(r"model name\s*:\s*(.+)", f.read())
            cpu = m.group(1).strip() if m else None
    except OSError:
        pass
    return {"kernel": kernel, "mesa": mesa or "unknown (pass --tag mesa=<version>)", "cpu_model": cpu, "cpus": os.cpu_count()}


class Host:
    """The real host side for one QEMU process. `root` is "" in production (tests point it at a fake tree)."""
    dry = False

    def __init__(self, pid, root="", live_conf=None, hz=None):
        self.pid, self.root, self.live_conf = pid, root, live_conf
        self.hz = hz or os.sysconf("SC_CLK_TCK")
        self.n = 0

    def qemu_pid(self):
        return self.pid

    def snap(self):
        import time
        return {"mono": time.monotonic(), "wall": time.time(), "pid": self.pid, "threads": thread_snapshot(self.pid, self.root),
                "gpu": gpu_render_ns(self.pid, self.root), "mem": mem_snapshot(self.pid, self.root)}

    def mem_avail_gb(self):
        v = mem_snapshot(self.pid, self.root).get("mem_avail_kb")
        return None if v is None else v / 1048576.0

    def disk_free_gb(self, path):
        try:
            st = os.statvfs(path)
            return st.f_bavail * st.f_frsize / 1073741824.0
        except OSError:
            return None

    def kernel_gpu(self, since):
        return kernel_gpu_lines(since)

    def same_qemu(self):
        return os.path.exists("%s/proc/%d" % (self.root, self.pid))

    def env(self):
        sw = read_live_conf(self.live_conf) if self.live_conf else {}
        return host_versions(), sw, qemu_env_switches(self.pid, self.root)
