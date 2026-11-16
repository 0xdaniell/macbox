"""Fixtures for the tests: synthetic screenshots and a synthetic scale result. No VM, no network."""
from macbench import png


def card_image(width=603, tear_at=None, with_cards=True):
    """A screenshot like GlassBench's at scale s = width/1206: white background, cards 336*s apart whose right edge
    strip (x 1125..1150 * s) is a vertical gradient (1 level per row at 3x), gaps 58*s tall. tear_at=y shifts every
    row below y by 40*s rows (a band from another frame)."""
    s = width / 1206.0
    h = int(2622 * s)
    pitch, gap = 336 * s, 58 * s
    rows = []
    for y in range(h):
        yy = y
        if tear_at is not None and y >= tear_at:
            yy = y + int(40 * s)
        k = (yy - int(300 * s)) % pitch if yy >= 300 * s else None
        row = bytearray([255] * (width * 3))
        if with_cards and k is not None and gap / 2 <= k < pitch - gap / 2 and 300 * s < yy < 2300 * s:
            level = int(40 + (k - gap / 2) / (pitch - gap) * 150)
            for x in range(width):
                if int(1125 * s) - 10 <= x < int(1150 * s) + 10:
                    row[x * 3:x * 3 + 3] = bytes([level, level // 2, 200 - level // 2])
                else:
                    row[x * 3:x * 3 + 3] = bytes([60, 60, 200])
        rows.append(bytes(row))
    return png.Image(width, h, 3, rows)


def strip_image(counter, width=402 * 3):
    """An image with GlassBench's strip showing `counter`."""
    s = width / 402.0
    h = int(900 * s)
    rows = [bytearray([128] * (width * 3)) for _ in range(h)]

    def block(i, white):
        x0, x1 = int((21 + 20 * i) * s), int((21 + 20 * i + 20) * s)
        for y in range(int(120 * s), int(140 * s)):
            for x in range(x0, x1):
                rows[y][x * 3:x * 3 + 3] = bytes([255, 255, 255] if white else [0, 0, 0])
    block(0, True)
    block(1, False)
    for b in range(16):
        block(b + 2, bool((counter >> b) & 1))
    return png.Image(width, h, 3, [bytes(r) for r in rows])


def fake_scale_result(ns=(1, 2, 3), fps=(60, 58, 40), mix="all-busy"):
    steps = []
    for n, f in zip(ns, fps):
        sims = [{"idx": i, "role": "busy", "fps": float(f), "p50_ms": 16.7, "p95_ms": 20.0 + (60 - f), "p99_ms": 30.0,
                 "max_ms": 50.0, "over2x": 2, "longest_gap_s": 0.05, "input_p95_ms": 100.0, "guest_cpu_pct": 30.0,
                 "guest_rss_mb": 800.0} for i in range(n)]
        steps.append({"n": n, "sims": sims, "gates_ok": True, "stable": True,
                      "system": {"qemu_cpu_cores": 4.0 + n, "gpu_busy_pct": 30.0 * n, "gpu_ms_per_frame": 6.8,
                                 "guest": {"mem_used_mb": 8000.0 + 900 * n, "compressed_mb": 100.0, "sims_cpu_pct": 30.0 * n,
                                           "other_cpu_pct": 20.0, "sims_rss_mb": 800.0 * n}},
                      "stability": {"ok": True}, "gates": {}})
    from macbench import analysis
    summary = analysis.summarize_scale(steps, ["smooth60: fps>=55, p95<=25", "smooth30: fps>=30, p95<=50"])
    return {"schema": 1, "tool": "macbox-bench", "kind": "scale", "id": "20260101-000000", "created": "2026-01-01T00:00:00Z",
            "params": {}, "env": {"build_id": "test"}, "sections": [], "gates": {},
            "data": {"mix": mix, "scale_x": 1, "ns": list(ns), "nominal_fps": 60, "steps": steps, "summary": summary}}
