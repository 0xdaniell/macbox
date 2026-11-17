"""Reaching the guest: ssh to the Mac (or the same helper run locally on stubs for DRY=1)."""
import json
import os
import shlex
import subprocess
import sys
import time

from .runctx import DeadlineError

GUEST_DIR = "~/.macbox/bench"


class GuestError(Exception):
    pass


class Pending:
    """A helper call running in the background: .wait() gives its JSON. The process is one of the run's children."""

    def __init__(self, guest, p, what, timeout):
        self.guest, self.p, self.what, self.timeout, self.t0 = guest, p, what, timeout, time.monotonic()

    def wait(self):
        left = max(1, self.timeout - (time.monotonic() - self.t0))
        try:
            out, err = self.p.communicate(timeout=left)
        except subprocess.TimeoutExpired:
            self.guest.run.kill_children()
            raise GuestError("%s: no answer in %ds" % (self.what, self.timeout))
        return self.guest.parse(self.what, self.p.returncode, out, err)


class SshGuest:
    def __init__(self, run, ssh_host, set_path, stubs_dir=None):
        self.run, self.ssh_host, self.set_path = run, ssh_host, set_path
        self.calls = 0
        self.fault_ssh_timeout = None      # DRY: fail the k-th call (tests)

    def argv(self, args):
        remote = "cd %s && BENCH_SET=\"$HOME/.macbox/bench/sets/%s\" xcrun python3 guest/benchguest.py %s" % (
            GUEST_DIR, self.set_path, " ".join(shlex.quote(str(a)) for a in args))
        return ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", self.ssh_host, remote]

    def parse(self, what, rc, out, err):
        lines = [l for l in (out or "").strip().splitlines() if l.startswith("{")]
        if not lines:
            raise GuestError("%s: rc %s: %s" % (what, rc, (err or out or "no output").strip()[-200:]))
        d = json.loads(lines[-1])
        if isinstance(d, dict) and "error" in d:
            raise GuestError("%s: %s" % (what, d["error"]))
        return d

    def _check(self, what, timeout, counted=True):
        if self.run.in_cleanup:
            return min(timeout, 240)
        if self.run.expired.is_set():
            raise DeadlineError("stopping: %s not started" % what)
        if self.run.remaining() < 5:
            raise DeadlineError("deadline: %s not started" % what)
        if counted:
            self.calls += 1
            if self.fault_ssh_timeout and self.calls == self.fault_ssh_timeout:
                raise GuestError("%s: ssh timed out (injected)" % what)
        return min(timeout, max(5, int(self.run.remaining())))

    def start(self, args, timeout=300, counted=True):
        what = args[0]
        timeout = self._check(what, timeout, counted)
        p = self.run.popen(self.argv(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return Pending(self, p, what, timeout)

    def call(self, args, timeout=300, counted=True):
        return self.start(args, timeout, counted).wait()

    def clock_offset(self):
        """Guest clock minus host clock in seconds, from the quickest of 5 round trips."""
        best = None
        for _ in range(5):
            t1 = time.time()
            try:
                p = subprocess.run(self.argv_plain("python3 -c 'import time; print(time.time())'"), capture_output=True, text=True, timeout=20)
                g = float(p.stdout.strip().splitlines()[-1])
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                continue
            t2 = time.time()
            if best is None or t2 - t1 < best[0]:
                best = (t2 - t1, g - (t1 + t2) / 2)
        return best[1] if best else 0.0

    def argv_plain(self, cmd):
        return ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", self.ssh_host, cmd]

    def alive(self):
        """The Mac answers over ssh (a monitor's question: it does not count as one of the run's calls)."""
        try:
            return subprocess.run(self.argv_plain("true"), capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _env(self):
        return None

    def quick(self, args, timeout=60):
        """A helper call for the monitors: not counted, never injected with the DRY faults of the run's own calls, and a
        timeout kills only itself (a hung monitor must not take the run's own call down with it)."""
        try:
            p = subprocess.run(self.argv(args), capture_output=True, text=True, timeout=timeout, env=self._env(), cwd=getattr(self, "bench_dir", None))
        except subprocess.TimeoutExpired:
            raise GuestError("%s: no answer in %ds" % (args[0], timeout))
        return self.parse(args[0], p.returncode, p.stdout, p.stderr)

    def sh(self, cmd, timeout=120):
        """A plain command in the guest: (rc, stdout)."""
        if self.run.expired.is_set() and not self.run.in_cleanup:
            raise DeadlineError("stopping")
        p = subprocess.run(self.argv_plain(cmd), capture_output=True, text=True, timeout=min(timeout, max(5, int(self.run.remaining()))))
        return p.returncode, p.stdout


class LocalGuest(SshGuest):
    """DRY=1: the same helper, run here with its stub primitives. State in a directory, so calls share one fake guest."""

    def __init__(self, run, set_path, state_dir, bench_dir, faults=""):
        super().__init__(run, "stub", set_path)
        self.state_dir, self.bench_dir, self.faults = state_dir, bench_dir, faults
        self.set_dir = os.path.join(state_dir, "set")

    def argv(self, args):
        return [sys.executable, os.path.join(self.bench_dir, "guest", "benchguest.py"), "--set", self.set_dir] + [str(a) for a in args]

    def start(self, args, timeout=300, counted=True):
        what = args[0]
        timeout = self._check(what, timeout, counted)
        p = self.run.popen(self.argv(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env(), cwd=self.bench_dir)
        return Pending(self, p, what, timeout)

    def _env(self):
        return dict(os.environ, BENCHGUEST_STUB="1", BENCHGUEST_STATE=self.state_dir, BENCH_STUB_FAULT=self.faults,
                    BENCHGUEST_CALLS=os.path.join(self.state_dir, "calls.log"), PYTHONPATH=self.bench_dir)

    def clock_offset(self):
        return 0.0

    def alive(self):
        return True

    def sh(self, cmd, timeout=120):
        return 0, ""
