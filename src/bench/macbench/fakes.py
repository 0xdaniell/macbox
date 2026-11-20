"""Stand-ins for DRY=1 and the tests: a fake Linux host (QEMU CPU, GPU, memory) and fake Coherent gates. The fake
numbers follow the number of booted simulators so every code path that reads them runs; they mean nothing."""
import os
import time


class FakeHost:
    dry = True

    def __init__(self, faults=""):
        self.n = 0
        self.faults = faults.split(",")
        self.t_last = time.monotonic()
        self.cpu_ticks = {"vcpu": 0, "sc": 0}
        self.gpu = 0
        self.pid = 4242
        self.kernel_lines = 0

    def qemu_pid(self):
        return self.pid

    def snap(self):
        now = time.monotonic()
        dt, self.t_last = now - self.t_last, now
        self.cpu_ticks["vcpu"] += int(dt * 100 * (0.05 + 0.8 * self.n))
        self.cpu_ticks["sc"] += int(dt * 100 * (0.01 + 0.05 * self.n))
        self.gpu += int(dt * 1e9 * min(0.95, 0.15 * self.n))
        v, s = self.cpu_ticks["vcpu"], self.cpu_ticks["sc"]
        thr = {"1": ("CPU 0/KVM", v - v // 5, v // 10, v // 2), "2": ("metal2vulkan-sc", s, s // 4, 0)}
        return {"mono": now, "wall": time.time(), "pid": self.pid, "threads": thr, "gpu": self.gpu,
                "mem": {"mem_avail_kb": int((40 - 0.5 * self.n) * 1048576), "qemu_rss_kb": int((9 + 0.4 * self.n) * 1048576),
                        "pswpin": 0, "pswpout": 0, "cg_swap": 0, "psi_some_us": 0}}

    def mem_avail_gb(self):
        v = 40 - 0.5 * self.n
        mf = [f for f in self.faults if f.startswith("hostmem@")]
        if mf and self.n >= int(mf[0].split("@")[1]):
            return 0.5
        return v

    def _fault_n(self, name):
        for f in self.faults:
            if f == name or f.startswith(name + "@"):
                return int(f.split("@")[1]) if "@" in f else 0
        return None

    def disk_free_gb(self, path):
        """Faults: diskstart (16 GiB from the start: under the 18 to start), disklow@N (12.5 from N simulators: under the 14 to
        boot another), diskabort@N (9: under the 11 to go on), diskunreadable."""
        if self._fault_n("diskunreadable") is not None:
            return None
        for name, val in (("diskabort", 9.0), ("disklow", 12.5)):
            k = self._fault_n(name)
            if k is not None and self.n >= k:
                return val
        if self._fault_n("diskstart") is not None:
            return 16.0
        return 500.0

    def kernel_gpu(self, since):
        if any(f.startswith("kernelgpu@") and self.n >= int(f.split("@")[1]) for f in self.faults):
            return 2, "fence expired (fake)"
        return 0, ""

    def same_qemu(self):
        k = self._fault_n("qemugone")           # the VM restarted: another QEMU process
        return not (k is not None and self.n >= k)

    def env(self):
        return {"kernel": "fake", "mesa": "fake", "cpu_model": "fake", "cpus": 20}, {"surface_coherent": "1"}, {"REIMS_VGPU_COHERENT": "1"}


class FakeCoherent:
    def __init__(self, host, faults=""):
        self.host, self.faults = host, faults.split(",")

    def mark(self):
        return {"n": 0}

    def check(self, mark):
        bad = any(f.startswith("coherent@") and self.host.n >= int(f.split("@")[1]) for f in self.faults)
        ok = lambda d: {"status": "PASS", "detail": d}
        return {"coherent_census": {"status": "FAIL", "detail": "overwrite_risk 1 (fake)"} if bad else ok("fake"),
                "coherent_process": ok("fake"), "qemu_log": ok("fake"), "gpu_hang": ok("fake")}
