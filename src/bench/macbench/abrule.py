"""The A/B decision rule (docs: README "A/B discipline").

Arms run interleaved A B B A inside one window; only the difference inside that window counts. Two pairs:
pair 1 = the first A with the first B, pair 2 = the second B with the second A. The rule is fixed BEFORE the run
(threshold on the command line) and has no re-run: a result below the bar, or with pairs that disagree, CLOSES
the lever."""
from . import stats

DEFAULT_THRESHOLD = 0.04     # the run-to-run noise on this stack


def pair_effects(a1, b1, a2, b2, higher_better=True):
    """Relative improvement of B over A in each pair (positive = B better). None where A is 0 or missing."""
    def eff(a, b):
        d = stats.rel_diff(a, b)
        return None if d is None else (d if higher_better else -d)
    return eff(a1, b1), eff(a2, b2)


def decide(a1, b1, a2, b2, higher_better=True, threshold=DEFAULT_THRESHOLD, tails=None, tail_tol=DEFAULT_THRESHOLD):
    """tails: {name: (a1, b1, a2, b2, higher_better)} metrics that must not get worse (p95, max, gaps...).
    Returns {"decision": "ADOPT"|"CLOSE", "effect", "pairs", "why": [...], "tails": {...}}"""
    e1, e2 = pair_effects(a1, b1, a2, b2, higher_better)
    why = []
    if e1 is None or e2 is None:
        return {"decision": "CLOSE", "effect": None, "pairs": [e1, e2], "why": ["a pair has no usable value"], "tails": {}}
    effect = (e1 + e2) / 2
    agree = (e1 > 0 and e2 > 0) or (e1 < 0 and e2 < 0)
    adopt = True
    if not agree:
        adopt = False
        why.append("the pairs disagree (%+.1f%%, %+.1f%%)" % (e1 * 100, e2 * 100))
    if effect <= threshold:
        adopt = False
        why.append("effect %+.1f%% does not exceed %.1f%%" % (effect * 100, threshold * 100))
    tail_out = {}
    for name, (ta1, tb1, ta2, tb2, hb) in (tails or {}).items():
        t1, t2 = pair_effects(ta1, tb1, ta2, tb2, hb)
        te = None if t1 is None or t2 is None else (t1 + t2) / 2
        worse = te is not None and te < -tail_tol
        tail_out[name] = {"effect": te, "worse": worse, "measured": te is not None}
        if te is None:          # a tail the rule asked for but no arm pair measured cannot permit an adoption
            adopt = False
            why.append("tail %s not measured" % name)
        elif worse:
            adopt = False
            why.append("tail %s worsens %+.1f%%" % (name, te * 100))
    return {"decision": "ADOPT" if adopt else "CLOSE", "effect": effect, "pairs": [e1, e2], "why": why, "tails": tail_out}


def abba_order():
    return ["A", "B", "B", "A"]


def decision_line(knob, metric, arms, res, threshold):
    """One human line, the one stored in the result."""
    f = lambda x: "n/a" if x is None else "%.2f" % x
    pairs = ", ".join("n/a" if p is None else "%+.1f%%" % (p * 100) for p in res["pairs"])
    eff = "n/a" if res["effect"] is None else "%+.1f%%" % (res["effect"] * 100)
    why = ("; " + "; ".join(res["why"])) if res["why"] else ""
    return "AB %s: %s | %s A %s/%s B %s/%s | effect %s (pairs %s) | bar %.1f%%%s" % (
        knob, res["decision"], metric, f(arms[0][0]), f(arms[0][1]), f(arms[1][0]), f(arms[1][1]), eff, pairs,
        threshold * 100, why)
