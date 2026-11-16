"""Quality targets ("every sim >= 55 distinct/s and p95 <= 25 ms") and the maximum sustainable N for each.

A target is `name: cond, cond, ...`; a cond is `<metric> <op> <number>` with op in >= <= > <.
Metrics (per simulator): fps (distinct app frames/s), p50, p95, p99, max (ms between distinct frames), over2x,
stall (longest gap between distinct frames, s), input_p50, input_p95 (ms, proxy; misses count at the timeout),
cpu (guest CPU % of one core), rss (MB). A metric a simulator has no value for (fps of an idle simulator,
the input proxy where no probe ran) is not judged for that simulator; a target with a condition that NO simulator of
an N has a value for is `None` (unknown), never a pass (unless another condition already failed it)."""
import re

METRICS = {"fps": "fps", "p50": "p50_ms", "p95": "p95_ms", "p99": "p99_ms", "max": "max_ms", "over2x": "over2x",
           "stall": "longest_gap_s", "input_p50": "input_p50_ms", "input_p95": "input_p95_ms",
           "cpu": "guest_cpu_pct", "rss": "guest_rss_mb"}
OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}

DEFAULT_TARGETS = [
    "smooth60: fps>=55, p95<=25",
    "smooth30: fps>=30, p95<=50",
    "responsive: input_p95<=150, stall<0.3",
]
_COND = re.compile(r"^\s*([a-z0-9_]+)\s*(>=|<=|>|<)\s*([0-9.]+)\s*$")


def parse_target(spec):
    """'name: fps>=55, p95<=25' -> {"name", "conds": [(metric, op, value)]}"""
    if ":" not in spec:
        raise ValueError("target %r: expected name: cond, cond" % spec)
    name, rest = spec.split(":", 1)
    conds = []
    for c in rest.split(","):
        m = _COND.match(c)
        if not m or m.group(1) not in METRICS:
            raise ValueError("target %r: bad condition %r (metrics: %s)" % (spec, c.strip(), ", ".join(METRICS)))
        conds.append((m.group(1), m.group(2), float(m.group(3))))
    if not conds:
        raise ValueError("target %r has no condition" % spec)
    return {"name": name.strip(), "conds": conds}


def evaluate(target, sims):
    """target against one N's per-simulator dicts: {"pass": True|False|None, "failing": [...], "judged": int}"""
    failing, judged = [], 0
    unjudged = set(c[0] for c in target["conds"])
    for sim in sims:
        for metric, op, val in target["conds"]:
            v = sim.get(METRICS[metric])
            if v is None:
                continue
            judged += 1
            unjudged.discard(metric)
            if not OPS[op](v, val):
                failing.append({"sim": sim.get("idx"), "cond": "%s%s%g" % (metric, op, val), "value": v})
    if judged == 0 or (unjudged and not failing):
        # a condition no simulator has a value for is unknown, and so is the target (never a pass by default)
        return {"pass": None, "failing": failing, "judged": judged, "unjudged": sorted(unjudged)}
    return {"pass": not failing, "failing": failing, "judged": judged}


def max_sustainable(steps, target):
    """steps: [{"n", "sims": [...], "gates_ok": bool, "stable": bool}] in any order. The largest N such that this
    N and every smaller measured N passed the target with its gates ok and no instability. The first N that does
    not pass ends it (a pass above a fail is noise or luck, not sustainability).
    Returns {"max_n": int|None, "first_fail": int|None, "unknown_at": int|None, "evaluated": {n: pass}}"""
    best, first_fail, unknown, ev = None, None, None, {}
    for st in sorted(steps, key=lambda s: s["n"]):
        r = evaluate(target, st["sims"])
        ok = r["pass"]
        if ok and not (st.get("gates_ok", True) and st.get("stable", True)):
            ok = False
        ev[st["n"]] = ok
        if first_fail is not None or unknown is not None:
            continue
        if ok:
            best = st["n"]
        elif ok is None:
            unknown = st["n"]
        else:
            first_fail = st["n"]
    return {"max_n": best, "first_fail": first_fail, "unknown_at": unknown, "evaluated": ev}
