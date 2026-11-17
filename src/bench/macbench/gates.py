"""Correctness gates. Each returns {"status": PASS|FAIL|SKIP, "detail": str}. An unreadable gate is SKIP, shown as
such, never PASS. Coherent and kernel gates are DELTAS over a window (a mark taken before, a check after), never
absolute counts or a fixed number of surfaces."""
import json
import os
import re

PROCESS_GATES = ("late_copies_in_busy", "late_beside_unplanned", "plan_left_planned")


def gate(status, detail=""):
    return {"status": status, "detail": detail}


# Crash reports that fail a run: the bench's own programs and the processes the measured frames pass through.
# macOS files reports for other daemons of a simulator all day (widget extensions, searchd, containermanagerd ...);
# those are listed with the gate, not judged, unless --strict-crashes says so.
CRASH_NAMES = ("GlassBench", "GenieBench", "simfps", "SpringBoard", "backboardd", "SimRenderServer", "SimMetalHost",
               "WindowServer", "com.apple.CoreSimulator.CoreSimulatorService", "CoreSimulatorBridge", "launchd_sim",
               "Simulator", "simctl", "Kernel", "JetsamEvent")
_CRASH_STAMP = re.compile(r"[-_.]\d{4}-\d{2}-\d{2}[-_]\d{6}.*$")


def crash_proc(report):
    """The process a crash report's file name names: 'searchd-2026-01-02-030405.ips' -> 'searchd'."""
    n = _CRASH_STAMP.sub("", report)
    return re.sub(r"\.(ips|crash)$", "", n)


def crash_split(reports, extra=(), strict=False):
    """(judged, listed): new crash reports that fail a gate, and those that are only shown."""
    names = {n.lower() for n in CRASH_NAMES} | {n.strip().lower() for n in extra if n.strip()}
    judged, listed = [], []
    for r in sorted(reports):
        (judged if strict or crash_proc(r).lower() in names else listed).append(r)
    return judged, listed


def crash_gate(reports, extra=(), strict=False):
    judged, listed = crash_split(reports, extra, strict)
    also = ("; not judged: %d other report(s): %s" % (len(listed), ", ".join(listed[:5]))) if listed else ""
    if judged:
        return gate("FAIL", "%d new crash report(s): %s%s" % (len(judged), ", ".join(judged[:3]), also))
    return gate("PASS", "no new crash report of the bench or the simulator's graphics path" + also)


def worst(gates):
    """FAIL if any FAIL; else SKIP-only/mixed is still ok: a run PASSes when nothing FAILed (SKIPs stay visible)."""
    return not any(g["status"] == "FAIL" for g in gates.values())


class Coherent:
    """Reads Coherent's census (coherent.jsonl, a report a minute per surface), QEMU's log and the GPU hang flag."""

    def __init__(self, census_dir, qemu_log, hang_file):
        self.census = os.path.join(census_dir, "coherent.jsonl") if census_dir else None
        self.qemu_log, self.hang_file = qemu_log, hang_file

    def _lines(self):
        out = []
        if self.census and os.path.exists(self.census):
            with open(self.census, errors="replace") as f:
                for l in f:
                    try:
                        out.append(json.loads(l))
                    except ValueError:
                        pass
        return out

    def _log_from(self, off):
        if not (self.qemu_log and os.path.exists(self.qemu_log)):
            return None
        with open(self.qemu_log, "rb") as f:
            f.seek(off)
            return f.read().decode(errors="replace")

    def mark(self):
        lines = self._lines()
        proc = {k: 0 for k in PROCESS_GATES}
        for d in lines:
            if "process" in d:
                for k in PROCESS_GATES:
                    proc[k] = max(proc[k], d.get(k, 0) or 0)
        size = os.path.getsize(self.qemu_log) if self.qemu_log and os.path.exists(self.qemu_log) else None
        return {"n": len(lines), "proc": proc, "log_off": size, "hang": bool(self.hang_file and os.path.exists(self.hang_file))}

    def check(self, mark):
        """Gates since the mark: overwrite_risk, sync_errors (new surface reports), process counters that rose,
        command buffer failures and GPU waits in QEMU's log, a new hang flag."""
        res = {}
        lines = self._lines()
        new = lines[mark["n"]:]
        surf = [d for d in new if "process" not in d]
        if self.census is None or not os.path.exists(self.census):
            res["coherent_census"] = gate("SKIP", "no census file (Coherent off?)")
        elif not surf:
            res["coherent_census"] = gate("SKIP", "no census report since the mark (they come once a minute)")
        else:
            risk = max((d.get("overwrite_risk", 0) or 0) for d in surf)
            errs = max((d.get("sync_errors", 0) or 0) for d in surf)
            res["coherent_census"] = gate("FAIL" if risk or errs else "PASS",
                                          "%d report(s): overwrite_risk max %d, sync_errors max %d" % (len(surf), risk, errs))
        proc = {k: mark["proc"][k] for k in PROCESS_GATES}
        for d in new:
            if "process" in d:
                for k in PROCESS_GATES:
                    proc[k] = max(proc[k], d.get(k, 0) or 0)
        rose = {k: proc[k] for k in PROCESS_GATES if proc[k] > mark["proc"][k]}
        if self.census and os.path.exists(self.census):
            res["coherent_process"] = gate("FAIL" if rose else "PASS", ("rose: %s" % rose) if rose else "late_copies_in_busy, late_beside_unplanned, plan_left_planned flat")
        else:
            res["coherent_process"] = gate("SKIP", "no census file")
        txt = self._log_from(mark["log_off"]) if mark["log_off"] is not None else None
        if txt is None:
            res["qemu_log"] = gate("SKIP", "QEMU log not readable")
        else:
            cb = txt.count("command buffer failed")
            waits = len(re.findall(r"waited \d+s for stamp", txt))
            res["qemu_log"] = gate("FAIL" if cb else "PASS", "command buffer failed %d, stamp waits %d" % (cb, waits))
        hang = bool(self.hang_file and os.path.exists(self.hang_file))
        if self.hang_file is None:
            res["gpu_hang"] = gate("SKIP", "no hang flag path")
        else:
            res["gpu_hang"] = gate("FAIL" if hang and not mark["hang"] else "PASS", "hang flag " + ("set" if hang else "clear"))
        return res


def kernel_gate(count, note):
    if count is None:
        return gate("SKIP", note)
    return gate("FAIL" if count else "PASS", "%d GPU error line(s)%s" % (count, (": " + note) if note else ""))


def strip_gate_of(sims):
    bad = [s for s in sims if s.get("strip_gate") and not s["strip_gate"]["ok"]]
    judged = [s for s in sims if s.get("strip_gate")]
    if not judged:
        return gate("SKIP", "no simulator drew the strip")
    if bad:
        return gate("FAIL", "; ".join("sim %s: %s" % (s["idx"], s["strip_gate"]["why"]) for s in bad[:3]))
    return gate("PASS", "%d simulator(s): no frame out of order" % len(judged))


def shots_gates(shots_by_sim, golden=None):
    """Tear, strip-readable and pixel-exact gates from the guest's `shots` output.
    shots_by_sim: [{"idx", "role", "shots": [{ok, hash, tear, tear_applicable, counter}]}]"""
    res = {}
    tear_bad, tear_judged = [], 0
    for s in shots_by_sim:
        if s["role"] != "busy":
            continue
        for sh in s["shots"]:
            if sh.get("ok") and sh.get("tear_applicable"):
                tear_judged += 1
                if sh["tear"]:
                    tear_bad.append(s["idx"])
    if not tear_judged:
        res["tears"] = gate("SKIP", "no screenshot showed the scrolling cards")
    else:
        res["tears"] = gate("FAIL" if tear_bad else "PASS", ("torn: sim %s" % sorted(set(tear_bad))) if tear_bad else "%d screenshot(s) clean" % tear_judged)
    statics = [sh for s in shots_by_sim if s["role"] == "static" for sh in s["shots"] if sh.get("ok")]
    hashes = [sh["hash"] for sh in statics]
    golden_hash, golden_key = golden(statics[0]["w"], statics[0]["h"]) if (golden and statics) else (None, None)
    if not hashes:
        res["pixels"] = gate("SKIP", "no static reference screen in this step")
    elif len(set(hashes)) > 1:
        res["pixels"] = gate("FAIL", "%d different pictures of the same static screen" % len(set(hashes)))
    elif golden_hash is None:
        res["pixels"] = gate("SKIP", "screens agree with each other; no golden stored for %s (bench golden record)" % (golden_key or "this setup"))
    else:
        res["pixels"] = gate("PASS" if hashes[0] == golden_hash else "FAIL",
                             "static screen %s the golden %s" % ("matches" if hashes[0] == golden_hash else "DIFFERS from", golden_key))
    return res


def leak_gate(name, xs, ys, bar_per_unit, unit):
    """FAIL when the least-squares slope of ys over xs exceeds the bar (e.g. MB per cycle) AND the growth is steady:
    at least 4 samples on a line (R2 >= 0.8). A slope above the bar from fewer samples or a scatter is one step, not a
    leak: SKIP, and it says to run longer."""
    from . import stats
    b, _, r2 = stats.slope(xs, ys)
    if b is None:
        return gate("SKIP", "%s: not enough samples" % name)
    detail = "%s: %+.2f %s (bar %g, R2 %.2f, %d samples)" % (name, b, unit, bar_per_unit, r2, len(xs))
    if b <= bar_per_unit:
        return gate("PASS", detail)
    if len(xs) < 4 or r2 < 0.8:
        return gate("SKIP", detail + ": above the bar but not a steady growth; run more cycles or longer to judge")
    return gate("FAIL", detail)
