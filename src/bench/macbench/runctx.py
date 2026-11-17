"""One run's safety: a hard deadline, explicit child pids, knobs that are put back, and ONE cleanup that runs on
every way out (normal end, error, SIGTERM/SIGINT, the deadline). Nothing waits on `wait` without a pid."""
import atexit
import os
import signal
import subprocess
import sys
import threading
import time


class DeadlineError(Exception):
    pass


class Run:
    def __init__(self, deadline_s, log=print, margin=60.0):
        self.t0 = time.monotonic()
        self.deadline = self.t0 + deadline_s
        self.margin = margin     # the watchdog acts this long before the deadline
        self.log = log
        self.children = []       # subprocess.Popen objects this run started
        self.undo = []           # (label, fn) restoring a knob, run last-in first-out
        self.teardown = []       # (label, fn): shut down what the run started (simulators)
        self.on_deadline = None  # fn: write the partial result
        self.cleaned = False
        self.in_cleanup = False
        self._lock = threading.Lock()
        self.expired = threading.Event()
        self.problems = []       # things cleanup found it could not restore
        self.aborted = None      # (reason, exit code) once a monitor ended the run
        self.on_abort = None     # fn(reason): records the abort in the result
        self.final = None        # fn(): after the cleanup, writes the result's closing sections (the cleanup's verdict)
        self._done = threading.Event()

    # --- time
    def remaining(self):
        return self.deadline - time.monotonic()

    def need(self, secs, what):
        """Refuse a step that would run past the deadline (the cleanup keeps its own 90 s)."""
        if self.remaining() < secs + self.margin + 30:
            raise DeadlineError("no time for %s (%d s needed, %d s left before the deadline)" % (what, secs, max(0, self.remaining() - self.margin - 30)))

    # --- children
    def popen(self, argv, **kw):
        p = subprocess.Popen(argv, start_new_session=True, **kw)
        with self._lock:
            self.children.append(p)
        return p

    def kill_children(self):
        for p in list(self.children):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        t_end = time.monotonic() + 3
        for p in list(self.children):
            try:
                p.wait(max(0.1, t_end - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        self.children.clear()

    # --- the one trap
    def install(self):
        atexit.register(self.cleanup)

        def on_signal(signum, frame):
            self.log("signal %d: stopping" % signum)
            self.expired.set()
            sys.exit(128 + signum)           # runs atexit -> cleanup
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        t = threading.Thread(target=self._watchdog, daemon=True)
        t.start()

    def _watchdog(self):
        while time.monotonic() < self.deadline - self.margin:
            time.sleep(1)
        self.log("DEADLINE: %d s left, stopping" % self.margin)
        self.expired.set()
        if self.on_deadline:
            try:
                self.on_deadline()
            except Exception as e:      # the partial result is best effort
                self.log("partial result failed: %s" % e)
        self.cleanup()
        if self.final:                  # the cleanup's verdict goes into the result of a run that ran out of time too
            try:
                self.final()
            except Exception as e:
                self.log("closing the result failed: %s" % e)
        os._exit(124)

    # --- monitors: what must stop the run while a long call is waiting
    def watch(self, name, fn, every):
        """Calls fn() every `every` seconds on a thread until the run is over; fn returns None (all well) or
        (reason, exit code): the run is then aborted: partial result, cleanup, exit."""
        def loop():
            while not self.expired.wait(every):
                try:
                    r = fn()
                except Exception as e:           # a check that cannot run is not a verdict
                    self.log("monitor %s: %s: %s" % (name, type(e).__name__, e))
                    continue
                if r:
                    self.abort(*r)
                    return
        t = threading.Thread(target=loop, daemon=True, name="watch-" + name)
        t.start()
        return t

    def abort(self, reason, code):
        """End the run NOW from a monitor thread: record why, write the partial result, run the one cleanup, write the
        closing sections, exit with `code`. The main thread, which may be blocked in a call, is killed with its children."""
        with self._lock:
            if self.aborted or self.cleaned:
                return
            self.aborted = (reason, code)
        self.log("ABORT: %s" % reason)
        self.expired.set()
        for hook, arg in ((self.on_abort, (reason,)), (self.on_deadline, (reason,))):
            if hook:
                try:
                    hook(*arg)
                except Exception as e:
                    self.log("abort hook failed: %s" % e)
        self.cleanup()
        if self.final:
            try:
                self.final()
            except Exception as e:
                self.log("closing the result failed: %s" % e)
        os._exit(code)

    def join_abort(self):
        """Called by the main thread after its own cleanup: when a monitor aborted the run, it owns the exit."""
        if self.aborted:
            time.sleep(600)

    def cleanup(self):
        with self._lock:
            if self.cleaned:
                already = True
            else:
                already = False
                self.cleaned = True
        if already:
            self._done.wait(900)            # another thread is cleaning up: do not exit before it is done
            return
        self.expired.set()
        self.in_cleanup = True
        self.kill_children()
        for label, fn in reversed(self.teardown):
            try:
                fn()
                self.log("cleanup: %s" % label)
            except Exception as e:
                self.problems.append("%s: %s" % (label, e))
                self.log("cleanup FAILED: %s: %s" % (label, e))
        for label, fn in reversed(self.undo):
            try:
                fn()
                self.log("restored: %s" % label)
            except Exception as e:
                self.problems.append("%s: %s" % (label, e))
                self.log("restore FAILED: %s: %s" % (label, e))
        self._done.set()


class LiveKnob:
    """A key=value line in the VM's live switch file: A = the file as it was, B = with the line set. The file's
    original text is kept and put back whole on any exit."""

    def __init__(self, path, key, value, run):
        self.path, self.key, self.value, self.run = path, key, value, run
        with open(path) as f:
            self.original = f.read()
        run.undo.append(("live switches file", self.restore))

    def _write(self, text):
        tmp = self.path + ".bench.tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, self.path)

    def set(self):
        lines = [l for l in self.original.splitlines() if not l.strip().startswith(self.key + "=")]
        self._write("\n".join(lines + ["%s=%s" % (self.key, self.value)]) + "\n")

    def unset(self):
        self._write(self.original)

    def restore(self):
        self._write(self.original)

    def describe(self):
        return "live:%s=%s" % (self.key, self.value)


class CmdKnob:
    """Shell commands for the knob (set / unset) run on the host. The unset command also runs on cleanup."""

    def __init__(self, set_cmd, unset_cmd, run, runner):
        self.set_cmd, self.unset_cmd, self.run, self.runner = set_cmd, unset_cmd, run, runner
        run.undo.append(("knob unset command", self.restore))

    def set(self):
        self.runner(self.set_cmd)

    def unset(self):
        self.runner(self.unset_cmd)

    def restore(self):
        self.runner(self.unset_cmd)

    def describe(self):
        return "cmd"


def shell_runner(timeout=60):
    def run_cmd(cmd):
        p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError("knob command failed (rc %d): %s" % (p.returncode, p.stderr.strip()[:120]))
    return run_cmd
