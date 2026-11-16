"""compare two result files: what moved between a baseline and a candidate, beyond the noise."""
from . import stats

NOISE = 0.04     # run-to-run noise on this stack

# (label, getter on a step, higher is better)
STEP_ROWS = [
    ("min distinct fps", lambda st: _min([s.get("fps") for s in st["sims"] if s.get("role") in ("busy", "agent")]), True),
    ("worst p95 ms", lambda st: _max([s.get("p95_ms") for s in st["sims"] if s.get("role") in ("busy", "agent")]), False),
    ("worst p99 ms", lambda st: _max([s.get("p99_ms") for s in st["sims"] if s.get("role") in ("busy", "agent")]), False),
    ("longest gap s", lambda st: _max([s.get("longest_gap_s") for s in st["sims"]]), False),
    ("input p95 ms", lambda st: _max([s.get("input_p95_ms") for s in st["sims"]]), False),
    ("Jain index", lambda st: (st.get("fairness") or {}).get("jain"), True),
    ("QEMU cores", lambda st: st["system"].get("qemu_cpu_cores"), False),
    ("GPU busy %", lambda st: st["system"].get("gpu_busy_pct"), False),
    ("GPU ms/frame", lambda st: st["system"].get("gpu_ms_per_frame"), False),
    ("guest mem used MB", lambda st: (st["system"].get("guest") or {}).get("mem_used_mb"), False),
]


def _vals(xs):
    return [x for x in xs if x is not None]


def _min(xs):
    v = _vals(xs)
    return min(v) if v else None


def _max(xs):
    v = _vals(xs)
    return max(v) if v else None


def verdict(a, b, higher_better, noise=NOISE):
    d = stats.rel_diff(a, b)
    if d is None:
        return "n/a", None
    if abs(d) <= noise:
        return "same", d
    better = d > 0 if higher_better else d < 0
    return ("better" if better else "WORSE"), d


def compare_scale(a, b, noise=NOISE):
    """Lines and a worse-count for two scale results. Rows are per N present in both."""
    lines, worse = [], 0
    da, db = a["data"], b["data"]
    lines.append("A %s  B %s   mix %s/%s  scale %sx/%sx  noise bar %.0f%%" % (
        a["id"], b["id"], da.get("mix"), db.get("mix"), da.get("scale_x"), db.get("scale_x"), noise * 100))
    if (da.get("mix"), da.get("scale_x")) != (db.get("mix"), db.get("scale_x")):
        lines.append("  warning: different mix or scale: the numbers are not comparable")
    for name in sorted(set(da["summary"]["max_n"]) | set(db["summary"]["max_n"])):
        ma = (da["summary"]["max_n"].get(name) or {}).get("max_n")
        mb = (db["summary"]["max_n"].get(name) or {}).get("max_n")
        tag = "same" if ma == mb else ("better" if (mb or 0) > (ma or 0) else "WORSE")
        worse += tag == "WORSE"
        lines.append("  max sustainable N  %-12s A %s  B %s   %s" % (name, ma, mb, tag))
    sa = {s["n"]: s for s in da["steps"]}
    sb = {s["n"]: s for s in db["steps"]}
    for n in sorted(set(sa) & set(sb)):
        lines.append("  N=%d" % n)
        for label, get, hb in STEP_ROWS:
            try:
                x, y = get(sa[n]), get(sb[n])
            except (KeyError, TypeError):
                continue
            v, d = verdict(x, y, hb, noise)
            if v == "n/a":
                continue
            worse += v == "WORSE"
            lines.append("    %-18s %10s -> %-10s %+6.1f%%  %s" % (label, _f(x), _f(y), d * 100, v))
    return lines, worse


def _f(x):
    return "n/a" if x is None else ("%.2f" % x if abs(x) < 100 else "%.0f" % x)


def compare_sections(a, b):
    sa = {s["name"]: s["status"] for s in a["sections"]}
    out = []
    for s in b["sections"]:
        was = sa.get(s["name"])
        mark = "" if was == s["status"] else "   (was %s)" % was
        out.append("  %s %s%s" % (s["status"], s["line"], mark))
    return out


def compare(a, b, noise=NOISE):
    """(text, regressions): regressions counts rows that got worse beyond the noise bar and sections that
    went from PASS to FAIL."""
    if a.get("kind") != b.get("kind"):
        return "different kinds: %s vs %s" % (a.get("kind"), b.get("kind")), 1
    lines, worse = [], 0
    if a["kind"] == "scale":
        l, worse = compare_scale(a, b, noise)
        lines += l
    lines.append("sections of B:")
    lines += compare_sections(a, b)
    sa = {s["name"]: s["status"] for s in a["sections"]}
    worse += sum(1 for s in b["sections"] if sa.get(s["name"]) == "PASS" and s["status"] == "FAIL")
    lines.append("%d regression(s) beyond the %.0f%% bar" % (worse, noise * 100))
    return "\n".join(lines), worse
