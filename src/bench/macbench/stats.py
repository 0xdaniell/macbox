"""Pure statistics: percentiles, frame intervals, fairness, slopes, motion stretches."""
import math


def percentile(values, p):
    """The value at fraction p (0..1) of the sorted values: sorted[min(n-1, int((n-1)*p))].
    The same rule as simfps and GlassBench's pace.json, so numbers agree across tools. [] -> None."""
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int((len(s) - 1) * p))]


def mean(values):
    return sum(values) / len(values) if values else None


def median(values):
    return percentile(values, 0.5)


def r1(x, digits=1):
    return None if x is None else round(x, digits)


def intervals_ms(times_s):
    """Gaps between consecutive times (seconds, ascending) in milliseconds."""
    return [(b - a) * 1000.0 for a, b in zip(times_s, times_s[1:])]


def frame_stats(times_s, nominal_fps=60.0, bounds=None):
    """Per-simulator frame metrics from the times (seconds) of its distinct frames.

    Without bounds:
    fps         (n - 1) / (last - first)
    p50..max    of the intervals between consecutive distinct frames, ms
    over2x      intervals longer than 2 / nominal_fps
    longest_gap_s  the longest interval in seconds

    bounds = (t0, t1), the stretch the observer watched: frames outside it are dropped, fps is n / (t1 - t0),
    and the gaps from t0 to the first frame and from the last frame to t1 count in max, over2x and the longest
    gap (not in the percentiles), so a simulator that froze before the window ended shows it.
    """
    if bounds:
        t0, t1 = bounds
        times_s = [t for t in times_s if t0 <= t <= t1]
    n = len(times_s)
    out = {"frames": n, "fps": 0.0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None,
           "over2x": 0, "longest_gap_s": None}
    iv = intervals_ms(times_s)
    edges = []
    if bounds:
        dur = t1 - t0
        out["fps"] = r1(n / dur if dur > 0 else 0.0)
        edges = [(times_s[0] - t0) * 1000.0, (t1 - times_s[-1]) * 1000.0] if n else [dur * 1000.0]
    elif n < 2:
        return out
    else:
        span = times_s[-1] - times_s[0]
        out["fps"] = r1((n - 1) / span if span > 0 else 0.0)
    if iv:
        out["p50_ms"] = r1(percentile(iv, 0.5))
        out["p95_ms"] = r1(percentile(iv, 0.95))
        out["p99_ms"] = r1(percentile(iv, 0.99))
    gaps = iv + edges
    if gaps:
        out["max_ms"] = r1(max(gaps))
        thr = 2000.0 / nominal_fps
        out["over2x"] = sum(1 for x in gaps if x > thr)
        out["longest_gap_s"] = r1(max(gaps) / 1000.0, 3)
    return out


def stretches(times_s, still_ms=250.0):
    """Split frame times into stretches of motion: a gap above still_ms ends a stretch (the screen stood
    still or the app froze that long). Returns [(first, last, count)] for each stretch of 2+ frames."""
    out, cur = [], []
    for t in times_s:
        if cur and (t - cur[-1]) * 1000.0 > still_ms:
            if len(cur) > 1:
                out.append((cur[0], cur[-1], len(cur)))
            cur = []
        cur.append(t)
    if len(cur) > 1:
        out.append((cur[0], cur[-1], len(cur)))
    return out


def jain(values):
    """Jain's fairness index (sum x)^2 / (n * sum x^2): 1.0 when every value is equal, 1/n when one
    simulator gets everything. None for no values; 1.0 for all zeros."""
    if not values:
        return None
    s2 = sum(v * v for v in values)
    if s2 == 0:
        return 1.0
    return sum(values) ** 2 / (len(values) * s2)


def min_max_ratio(values):
    """min / max (1.0 = equal). None for no values, 1.0 for all zeros."""
    if not values:
        return None
    hi = max(values)
    return 1.0 if hi == 0 else min(values) / hi


def slope(xs, ys):
    """Least-squares line through (x, y): (slope, intercept, r2). Needs 2+ distinct x, else (None, None, None).
    The per-simulator standing cost of a resource is the slope over N."""
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 2:
        return None, None, None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None, None, None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    b = sxy / sxx
    a = my - b * mx
    syy = sum((p[1] - my) ** 2 for p in pts)
    r2 = 1.0 if syy == 0 else (sxy * sxy) / (sxx * syy)
    return b, a, r2


def rel_diff(a, b):
    """(b - a) / a as a fraction; None when a is 0 or missing."""
    if a is None or b is None or a == 0:
        return None
    return (b - a) / a


def finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)
