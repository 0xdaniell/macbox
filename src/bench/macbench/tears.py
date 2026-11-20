"""Tear detector for GlassBench screenshots (ported from the private tears.py; no Pillow).

Each card is a vertical gradient, so the strip at the card's right edge (no text) changes by about one level
a row at 3x. A band of rows taken from another frame (the scroll moved meanwhile) shows as a step there, away
from card edges, and moves the cards' spacing away from 336 px (at 3x). Calibrated for GlassBench; it needs a
screenshot of the scrolling app (it says `applicable: false` when it finds no cards)."""


def _row_means(img, x0, x1, y0, y1):
    rows = []
    n = max(1, x1 - x0)
    for y in range(img.h):
        acc = [0, 0, 0]
        if y0 - 80 <= y <= y1 + 80:     # rows outside the checked band are not needed
            for x in range(x0, x1):
                r, g, b = img.px(x, y)
                acc[0] += r; acc[1] += g; acc[2] += b
        rows.append((acc[0] / n, acc[1] / n, acc[2] / n))
    return rows


def detect_tears(img):
    """{"tear": bool, "applicable": bool, "odd": [...], "steps": [(row, size)], "gaps": [heights]}"""
    w, h = img.w, img.h
    s = w / 1206.0
    x0, x1 = int(1125 * s), max(int(1125 * s) + 1, int(1150 * s))
    # The band starts below the navigation bar's glass, which ends at 420 px (3x): a gap that scrolls under the glass
    # is cut there and looked like a short gap at a wrong distance (a "tear" in every third screenshot).
    y0, y1 = int(440 * s), min(int(1700 * s), h - 1)
    rows = _row_means(img, x0, x1, y0, y1)
    gap = [min(c) > 225 for c in rows]            # the white background between cards
    margin = int(75 * s)
    near = [False] * h
    for y in range(h):
        if gap[y]:
            for k in range(max(0, y - margin), min(h, y + margin + 1)):
                near[k] = True
    thr = 6.0 if s > 0.9 else 6.0 / s * 0.5
    steps = []
    for y in range(y0 + 1, y1):
        if near[y] or near[y - 1]:
            continue
        d = max(abs(a - b) for a, b in zip(rows[y], rows[y - 1]))
        if d > thr:
            steps.append((y, round(d, 1)))
    gaps, y = [], y0
    while y < y1:
        if gap[y]:
            st = y
            while y < y1 and gap[y]:
                y += 1
            gaps.append((st, y - st))
        y += 1
    gaps = [g for g in gaps if g[0] > y0 + 1 and g[0] + g[1] < y1 - 1]    # whole gaps only
    spacing = [b[0] - a[0] for a, b in zip(gaps, gaps[1:])]
    exp, tol = 336 * s, max(2, 4 * s)
    odd = [x for x in spacing if abs(x - exp) > tol] + [g[1] for g in gaps if abs(g[1] - 58 * s) > tol]
    # As in the private tool, the card spacing and gap heights decide; steps in the gradient are reported only.
    return {"tear": bool(odd), "applicable": len(gaps) >= 2, "odd": odd, "steps": steps[:8],
            "gaps": [g[1] for g in gaps]}
