#!/usr/bin/env python3
"""Writes fps-light.svg and fps-dark.svg, the README's frame-rate chart (docs/performance.md's numbers).
Colors: the validated categorical slots 1 and 2 (blue, orange) for each mode's surface: orange for
upstream, blue for macbox's builds, the build macbox makes now at full strength and the earlier ones lighter."""
import os

ROWS = [  # label, sublabel, fps (3x, mean of the valid rounds), kind
    ("Upstream", "Reims 2ce3209, 2026-10-26", 17.2, "before"),
    ("macbox, Oct 28", "in place rendering", (38.5 + 39.5) / 2, "after"),
    ("macbox, Nov 5", "cohwl: flat32", 50.6, "after"),
    ("+ Coherent", "zero-copy surfaces, Nov 5", 70.0, "after"),
    ("macbox now", "Coherent + grouped completion", 120.0, "now"),
]
MODES = {
    "light": dict(surface="#fcfcfb", text="#0b0b0b", text2="#52514e", grid="#e4e3df", before="#eb6834", after="#2a78d6"),
    "dark": dict(surface="#1a1a19", text="#f0efec", text2="#c3c2b7", grid="#3a3a37", before="#d95926", after="#3987e5"),
}
W, H = 900, 380
X0, X1, MAX = 250, 830, 130          # bar area and its scale (fps)
BAR, GAP = 30, 16                    # bar thickness, space between bars
TOP = 96
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

def x(v): return X0 + (X1 - X0) * v / MAX

def bar(y, v, color, opacity=1.0):  # 4 px rounded at the data end only, square at the baseline
    w, r = x(v) - X0, 4
    return (f'<path d="M{X0},{y} H{X0 + w - r} Q{X0 + w},{y} {X0 + w},{y + r} V{y + BAR - r} '
            f'Q{X0 + w},{y + BAR} {X0 + w - r},{y + BAR} H{X0} Z" fill="{color}" fill-opacity="{opacity}"/>')

def svg(m):
    c = MODES[m]
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">',
         f'<rect width="{W}" height="{H}" rx="12" fill="{c["surface"]}"/>',
         f'<text x="24" y="38" font-size="19" font-weight="600" fill="{c["text"]}">iOS Simulator at 3x (1206×2622), Liquid Glass benchmark</text>',
         f'<text x="24" y="62" font-size="13.5" fill="{c["text2"]}">Frames per second on an Intel Arrow Lake iGPU (Core Ultra 7 265). Higher is better.</text>']
    bottom = TOP + len(ROWS) * BAR + (len(ROWS) - 1) * GAP
    for v in (0, 20, 40, 60, 80, 100, 120):
        o.append(f'<line x1="{x(v):.1f}" y1="{TOP - 6}" x2="{x(v):.1f}" y2="{bottom + 6}" stroke="{c["grid"]}" stroke-width="1"/>')
        o.append(f'<text x="{x(v):.1f}" y="{bottom + 24}" font-size="12" text-anchor="middle" fill="{c["text2"]}">{v}</text>')
    o.append(f'<text x="{X1}" y="{bottom + 42}" font-size="12" text-anchor="end" fill="{c["text2"]}">frames per second</text>')
    # 60 fps, and the iPhone 17 Pro's 120 Hz screen: the most its simulator can show
    for v, t in ((60, "60 fps"), (120, "120 Hz screen")):
        o.append(f'<line x1="{x(v):.1f}" y1="{TOP - 14}" x2="{x(v):.1f}" y2="{bottom + 6}" stroke="{c["text2"]}" stroke-width="1.5" stroke-dasharray="4 4"/>')
        o.append(f'<text x="{x(v) - 6:.1f}" y="{TOP - 4}" font-size="12" text-anchor="end" fill="{c["text2"]}">{t}</text>')
    y = TOP
    for label, sub, v, kind in ROWS:
        col = c["before"] if kind == "before" else c["after"]
        o.append(f'<text x="{X0 - 14}" y="{y + 13}" font-size="13.5" font-weight="600" text-anchor="end" fill="{c["text"]}">{label}</text>')
        o.append(f'<text x="{X0 - 14}" y="{y + 28}" font-size="11.5" text-anchor="end" fill="{c["text2"]}">{sub}</text>')
        o.append(bar(y, v, col, 1.0 if kind in ("before", "now") else 0.55))
        o.append(f'<text x="{x(v) + 8:.1f}" y="{y + BAR / 2 + 5}" font-size="13.5" font-weight="600" fill="{c["text"]}">{v:g}</text>')
        y += BAR + GAP
    o.append("</svg>")
    return "\n".join(o) + "\n"

here = os.path.dirname(os.path.abspath(__file__))
for m in MODES:
    open(os.path.join(here, f"fps-{m}.svg"), "w").write(svg(m))
