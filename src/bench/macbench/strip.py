"""GlassBench's frame strip: a 16-bit counter drawn on every display-link tick as 18 squares of 20 pt from
(21, 120) pt of a 402 pt wide screen: white and black references, then bits 0..15 (white = 1)."""
from . import stats

WRAP = 65536
MAX_FWD = 1000          # a forward step of up to this many ticks is a stall (frames lost); larger is a jump


def read_counter_luma(luma_at, width_px):
    """The counter from a luma function luma_at(x_px, y_px). Same geometry as simfps's readCounter.
    Returns the counter, or -1 when the references are unreadable (no strip, or it is covered)."""
    scale = width_px / 402.0

    def block(i):
        return luma_at(int((21 + 20 * i + 10) * scale), int(130 * scale))
    white, black = block(0), block(1)
    if white - black <= 100:
        return -1
    mid = (white + black) // 2
    n = 0
    for b in range(16):
        if block(b + 2) > mid:
            n |= 1 << b
    return n


def read_counter_image(img):
    return read_counter_luma(img.luma, img.w)


def analyze_counters(samples):
    """Distinct app frames from the strip samples [(t_seconds, counter or -1)] of one simulator.

    A sample whose counter equals the previous distinct one is a repeat (the compositor made another
    surface; normal). A counter that moved forward by d: d - 1 ticks never reached the screen (lost).
    A counter that moved BACKWARD by up to 1000 (an older frame shown after a newer one) or jumped
    forward by more than 1000 is `backwards` / `jumps`: never normal, and the strip gate fails on them.
    Returns {"unique": [(t, counter)], "repeats", "lost", "backwards", "jumps", "unreadable", "span_ticks"}
    where span_ticks is the counter distance covered, i.e. ticks the app made between first and last frame."""
    unique, repeats, lost, back, jumps, unreadable, span = [], 0, 0, 0, 0, 0, 0
    for t, c in samples:
        if c < 0:
            unreadable += 1
            continue
        if unique:
            d = (c - unique[-1][1]) % WRAP
            if d == 0:
                repeats += 1
                continue
            if d <= MAX_FWD:
                lost += d - 1
                span += d
            elif d >= WRAP - MAX_FWD:
                back += 1          # went back: do not advance the reference, the next sample is judged against it
                continue
            else:
                jumps += 1
                span += d
        unique.append((t, c))
    return {"unique": unique, "repeats": repeats, "lost": lost, "backwards": back, "jumps": jumps,
            "unreadable": unreadable, "span_ticks": span}


def strip_gate(analysis, app_ticks=None, max_unreadable_frac=0.01):
    """(ok, reason) of the strip gate.
    FAIL: a frame out of order (backwards), a jump, a counter that advanced more than the app says it ticked
    (reading garbage), or more than 1% of samples unreadable. Lost ticks are NOT a failure: they are the fps
    metric (a simulator that drops frames is slow, not wrong). Skips are judged against the app's own tick
    count (GlassBench's pace.json) when given."""
    a = analysis
    total = len(a["unique"]) + a["repeats"] + a["unreadable"] + a["backwards"]
    if a["backwards"]:
        return False, "%d frame(s) shown out of order" % a["backwards"]
    if a["jumps"]:
        return False, "%d counter jump(s) over %d ticks" % (a["jumps"], MAX_FWD)
    if app_ticks is not None and a["span_ticks"] > app_ticks * 1.02 + 2:
        return False, "counter advanced %d ticks but the app ticked %d times" % (a["span_ticks"], app_ticks)
    if total and a["unreadable"] / total > max_unreadable_frac:
        return False, "%d of %d samples unreadable" % (a["unreadable"], total)
    return True, "ok"


def distinct_metrics(samples, nominal_fps=60.0):
    """frame_stats of the distinct frames, plus the strip bookkeeping, for one simulator."""
    a = analyze_counters(samples)
    m = stats.frame_stats([t for t, _ in a["unique"]], nominal_fps)
    m.update({k: a[k] for k in ("repeats", "lost", "backwards", "jumps", "unreadable", "span_ticks")})
    return m
