"""The result file: one JSON document per run, schema versioned. Nothing in it names a host, an account or a path."""
import datetime
import json
import os
import re
import socket

SCHEMA = 1
KINDS = ("scale", "ab", "idle", "lifecycle", "build", "soak", "panel", "gates")
SECTION_STATUS = ("PASS", "FAIL", "SKIP", "INFO")

_PATH = re.compile(r"(?:^|[\s\"'=:(,])(/(?:srv|home|Users|var|tmp|private|etc|opt|mnt|root|usr|run|Volumes|proc|sys)/[^\s\"',)]*)")
_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def scrubber(extra_names=()):
    """A function that cleans a string: home paths -> ~, the host name, ssh alias and other names -> <host>,
    IPv4 addresses -> <ip>. Paths not under a home directory are left for validate() to refuse."""
    names = {n for n in [socket.gethostname(), socket.gethostname().split(".")[0], *extra_names] if n and len(n) > 2}
    home = os.path.expanduser("~")

    def clean(s):
        if home and home != "/":
            s = s.replace(home, "~")
        s = re.sub(r"/Users/[^/\s\"']+", "~", s)
        for n in sorted(names, key=len, reverse=True):
            s = re.sub(r"(?<![A-Za-z0-9_-])" + re.escape(n) + r"(?![A-Za-z0-9_-])", "<host>", s)
        return _IPV4.sub("<ip>", s)
    return clean


def scrub(obj, clean):
    if isinstance(obj, str):
        return clean(obj)
    if isinstance(obj, list):
        return [scrub(x, clean) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v, clean) for k, v in obj.items()}
    return obj


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _strings(x)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)


def privacy_problems(doc, extra_names=()):
    """Strings in a result that would not be safe to publish: absolute paths, IPv4 addresses, host names."""
    names = {n for n in [socket.gethostname(), socket.gethostname().split(".")[0], *extra_names] if n and len(n) > 2}
    bad = []
    for s in _strings(doc):
        if _PATH.search(s):
            bad.append("path: " + s[:80])
        if _IPV4.search(s):
            bad.append("ip: " + s[:80])
        for n in names:
            if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(n) + r"(?![A-Za-z0-9_-])", s):
                bad.append("host name in: " + s[:80])
    return bad


def new_result(kind, params, env, run_id=None):
    return {"schema": SCHEMA, "tool": "macbox-bench", "kind": kind, "id": run_id or stamp(), "created": now_utc(),
            "params": params, "env": env, "sections": [], "gates": {}, "data": {}}


def add_section(doc, name, status, line, detail=None):
    assert status in SECTION_STATUS, status
    doc["sections"].append({"name": name, "status": status, "line": line, "detail": detail})


def validate(doc):
    """Problems in a result document ([] = fine): shape, schema version and privacy."""
    p = []
    if not isinstance(doc, dict):
        return ["not an object"]
    if doc.get("schema") != SCHEMA:
        p.append("schema %r, expected %d" % (doc.get("schema"), SCHEMA))
    if doc.get("kind") not in KINDS:
        p.append("unknown kind %r" % doc.get("kind"))
    for k, t in (("id", str), ("created", str), ("params", dict), ("env", dict), ("sections", list), ("gates", dict),
                 ("data", dict)):
        if not isinstance(doc.get(k), t):
            p.append("%s: missing or not %s" % (k, t.__name__))
    for s in doc.get("sections", []) if isinstance(doc.get("sections"), list) else []:
        if s.get("status") not in SECTION_STATUS or not s.get("name") or "line" not in s:
            p.append("bad section %r" % (s.get("name"),))
    if doc.get("kind") == "scale":
        d = doc.get("data", {})
        for k in ("mix", "scale_x", "steps", "summary"):
            if k not in d:
                p.append("scale data lacks " + k)
        for st in d.get("steps", []):
            for k in ("n", "sims", "system", "stability", "gates_ok"):
                if k not in st:
                    p.append("step lacks " + k)
            for sim in st.get("sims", []):
                if "idx" not in sim or "role" not in sim:
                    p.append("simulator entry lacks idx/role")
    p += privacy_problems(doc)
    return p


def write(doc, path, clean=None):
    """Writes the result, always (a long run's data is never thrown away); problems found are listed in the
    file as "problems" and returned, and `publishable` says whether it may be committed as it is."""
    if clean is not None:
        doc = scrub(doc, clean)
    doc.pop("problems", None)
    problems = validate(doc)
    doc["publishable"] = not problems
    if problems:
        doc["problems"] = problems[:20]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return problems


def load(path):
    with open(path) as f:
        return json.load(f)
