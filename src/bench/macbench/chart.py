"""A self-contained HTML/SVG scaling chart from a scale result: no scripts, no fonts, no external files."""
import html

COLORS = ["#2a6fdb", "#d9534f", "#2e9e5b", "#e08a1e", "#8e5ac8", "#17a2b8", "#a0522d", "#6c757d"]
W, H, L, R, T, B = 460, 260, 52, 14, 28, 40


def _nice(hi):
    if hi <= 0:
        return 1.0
    import math
    e = 10 ** math.floor(math.log10(hi))
    for m in (1, 2, 2.5, 5, 10):
        if hi <= m * e:
            return m * e
    return 10 * e


def panel(title, xs, series, ylabel, hlines=(), ymax=None):
    """One SVG chart. series: [(name, [(x, y)], style)] style 'line' or 'dots'. hlines: [(y, label)]."""
    pts = [p for _, ps, _ in series for p in ps if p[1] is not None]
    hi = ymax or _nice(max([p[1] for p in pts] + [h[0] for h in hlines] + [0]) * 1.05 or 1)
    x0, x1 = min(xs), max(xs)
    span = (x1 - x0) or 1

    def px(x):
        return L + (x - x0) / span * (W - L - R) if x1 != x0 else (L + W - R) / 2

    def py(y):
        return H - B - (y / hi) * (H - T - B)
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="sans-serif" font-size="11">' % (W, H, W, H),
         '<rect width="%d" height="%d" fill="#fff"/>' % (W, H),
         '<text x="%d" y="16" font-weight="bold">%s</text>' % (L, html.escape(title))]
    for i in range(5):
        y = hi * i / 4
        o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#e6e6e6"/>' % (L, W - R, py(y), py(y)))
        o.append('<text x="%d" y="%.1f" text-anchor="end" fill="#555">%g</text>' % (L - 4, py(y) + 4, round(y, 2)))
    for x in xs:
        o.append('<text x="%.1f" y="%d" text-anchor="middle" fill="#555">%g</text>' % (px(x), H - B + 14, x))
    o.append('<text x="%d" y="%d" text-anchor="middle" fill="#555">simulators</text>' % ((L + W - R) // 2, H - 6))
    o.append('<text x="10" y="%d" fill="#555" transform="rotate(-90 10 %d)">%s</text>' % ((T + H - B) // 2 + 20, (T + H - B) // 2 + 20, html.escape(ylabel)))
    for y, label in hlines:
        o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#c33" stroke-dasharray="4 3"/>' % (L, W - R, py(y), py(y)))
        o.append('<text x="%d" y="%.1f" text-anchor="end" fill="#c33">%s</text>' % (W - R, py(y) - 3, html.escape(label)))
    for i, (name, ps, style) in enumerate(series):
        c = COLORS[i % len(COLORS)]
        ps = [p for p in ps if p[1] is not None]
        if style == "line" and len(ps) > 1:
            o.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (c, " ".join("%.1f,%.1f" % (px(x), py(y)) for x, y in ps)))
        for x, y in ps:
            o.append('<circle cx="%.1f" cy="%.1f" r="%d" fill="%s" fill-opacity="%s"><title>%s N=%g: %g</title></circle>' % (
                px(x), py(y), 3 if style == "line" else 2.5, c, "1" if style == "line" else "0.55", html.escape(name), x, y))
    lx = L + 6
    for i, (name, ps, style) in enumerate(series):
        o.append('<rect x="%d" y="%d" width="9" height="9" fill="%s"/><text x="%d" y="%d" fill="#333">%s</text>' % (lx, T - 12 + 0, COLORS[i % len(COLORS)], lx + 12, T - 4, html.escape(name)))
        lx += 12 + 6 * len(name) + 14
    o.append("</svg>")
    return "\n".join(o)


def _per_sim(steps, key, role="busy"):
    pts = []
    for st in steps:
        for s in st["sims"]:
            if s.get("role") == role and s.get(key) is not None:
                pts.append((st["n"], s[key]))
    return pts


def _agg(steps, fn, key, role="busy"):
    out = []
    for st in steps:
        v = [s[key] for s in st["sims"] if s.get("role") == role and s.get(key) is not None]
        out.append((st["n"], fn(v) if v else None))
    return out


def _sys(steps, *path):
    out = []
    for st in steps:
        v = st["system"]
        for p in path:
            v = v.get(p) if isinstance(v, dict) else None
        out.append((st["n"], v))
    return out


def scale_chart(doc):
    d = doc["data"]
    steps = sorted(d["steps"], key=lambda s: s["n"])
    xs = [s["n"] for s in steps]
    nominal = d.get("nominal_fps", 60)
    panels = [
        panel("distinct frames/s per simulator", xs, [("each sim", _per_sim(steps, "fps"), "dots"),
              ("min", _agg(steps, min, "fps"), "line"), ("mean", _agg(steps, lambda v: sum(v) / len(v), "fps"), "line")],
              "frames/s", hlines=[(nominal, "nominal %g" % nominal)]),
        panel("frame interval p95 / p99 (ms)", xs, [("p95 each", _per_sim(steps, "p95_ms"), "dots"),
              ("worst p95", _agg(steps, max, "p95_ms"), "line"), ("worst p99", _agg(steps, max, "p99_ms"), "line")],
              "ms", hlines=[(25, "25 ms")]),
        panel("input proxy p95 (ms)", xs, [("each sim", [(st["n"], s["input_p95_ms"]) for st in steps for s in st["sims"] if s.get("input_p95_ms") is not None], "dots"),
              ("worst", [(st["n"], max([s["input_p95_ms"] for s in st["sims"] if s.get("input_p95_ms") is not None] or [None])) for st in steps], "line")],
              "ms", hlines=[(150, "150 ms")]),
        panel("QEMU CPU (cores) and GPU busy (%)", xs, [("QEMU cores", _sys(steps, "qemu_cpu_cores"), "line"),
              ("GPU busy %", [(x, None if y is None else y / 10.0) for x, y in _sys(steps, "gpu_busy_pct")], "line")],
              "cores | % / 10"),
        panel("guest CPU of the simulators (%)", xs, [("simulators", _sys(steps, "guest", "sims_cpu_pct"), "line"),
              ("everything else", _sys(steps, "guest", "other_cpu_pct"), "line")], "% of one core"),
        panel("guest memory (MB)", xs, [("used", _sys(steps, "guest", "mem_used_mb"), "line"),
              ("compressed", _sys(steps, "guest", "compressed_mb"), "line"), ("simulators RSS", _sys(steps, "guest", "sims_rss_mb"), "line")], "MB"),
        panel("GPU ms per frame", xs, [("GPU ms/frame", _sys(steps, "gpu_ms_per_frame"), "line")], "ms"),
        panel("fairness (Jain index x 100)", xs, [("Jain x100", [(st["n"], None if st.get("fairness", {}).get("jain") is None else st["fairness"]["jain"] * 100) for st in steps], "line"),
              ("min/max x100", [(st["n"], None if st.get("fairness", {}).get("min_max") is None else st["fairness"]["min_max"] * 100) for st in steps], "line")],
              "index x100", ymax=100),
    ]
    mx = d.get("summary", {}).get("max_n", {})
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (html.escape(k), v.get("max_n"), v.get("first_fail")) for k, v in mx.items())
    return ('<!doctype html><meta charset="utf-8"><title>macbox bench %s</title>'
            '<body style="font-family:sans-serif;margin:16px"><h2>macbox bench: %s, %sx, %s</h2>'
            '<p>run %s, build %s</p><table border="1" cellpadding="4" style="border-collapse:collapse">'
            '<tr><th>target</th><th>max sustainable N</th><th>first failing N</th></tr>%s</table><div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">%s</div></body>'
            % (html.escape(doc["id"]), html.escape(str(d.get("mix"))), d.get("scale_x"), html.escape(str(d.get("device", ""))),
               html.escape(doc["id"]), html.escape(str(doc["env"].get("build_id"))), rows, "\n".join(panels)))
