#!/usr/bin/env python3
"""Drive the macOS VM through QEMU's QMP socket: screenshots, mouse and keyboard.
Runs inside the macbox container (`macbox screenshot/click/type/key/scroll/wait` call it).
VM_QMP is the QMP socket; VM_RESOLUTION_FILE holds the screen size clicks are scaled to.

  vmctl.py shot OUT.png            screendump of the guest display (PNG)
  vmctl.py click X Y [right|double] click at guest pixel X,Y (the screen's current size)
  vmctl.py move X Y
  vmctl.py scroll X Y N            wheel N notches (negative = up)
  vmctl.py type TEXT               type text (US layout)
  vmctl.py wait SECONDS [OUT.png]  wait until the screen changes (>1%), then shot
  vmctl.py key COMBO...            e.g. ret, tab, esc, meta-space, meta-q, ctrl-c
  vmctl.py find TEXT               OCR the screen: "X Y TEXT" for every place TEXT shows (exit 1: none)
  vmctl.py click-text TEXT [--nth N] [--right|--double]   click the Nth place TEXT shows (1: topmost)
  vmctl.py wait-text SECONDS TEXT  wait until TEXT shows, then print where

find/click-text/wait-text need tesseract in the image (macbox build). TEXT is one or more whole words,
matched case-insensitively, in order, on one line of the screen; X Y are guest pixels, like click's.
"""
import json, os, re, socket, struct, subprocess, sys, time

SOCK = os.environ["VM_QMP"]
SHOTS = os.environ.get("VM_SHOTS", "/tmp")
def _res():  # the guest's screen size ($MACBOX_HOME/run/resolution), 1920x1080 by default
    try: return tuple(int(v) for v in open(os.environ["VM_RESOLUTION_FILE"]).read().strip().split("x"))
    except Exception: return (1920, 1080)
W, H = int(os.environ.get("VM_W", _res()[0])), int(os.environ.get("VM_H", _res()[1]))

class QMP:
    def __init__(self):
        for _ in range(50):
            try:
                self.s = socket.socket(socket.AF_UNIX); self.s.connect(SOCK); break
            except OSError:
                time.sleep(0.1)
        self.f = self.s.makefile("rw")
        json.loads(self.f.readline())
        self.cmd("qmp_capabilities")
    def cmd(self, name, **args):
        self.f.write(json.dumps({"execute": name, "arguments": args} if args else {"execute": name}) + "\n"); self.f.flush()
        while True:
            r = json.loads(self.f.readline())
            if "return" in r: return r["return"]
            if "error" in r: raise SystemExit(f"{name}: {r['error']}")
    def ev(self, events):
        self.cmd("input-send-event", events=events)

def absm(x, y):
    return [{"type": "abs", "data": {"axis": "x", "value": int(x * 32767 / (W - 1))}},
            {"type": "abs", "data": {"axis": "y", "value": int(y * 32767 / (H - 1))}}]

def btn(b, down):
    return [{"type": "btn", "data": {"down": down, "button": b}}]

SHIFTED = {'~': '`', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
           '_': 'minus', '+': 'equal', '{': 'bracket_left', '}': 'bracket_right', '|': 'backslash', ':': 'semicolon',
           '"': 'apostrophe', '<': 'comma', '>': 'dot', '?': 'slash'}
PLAIN = {'`': 'grave_accent', '-': 'minus', '=': 'equal', '[': 'bracket_left', ']': 'bracket_right', '\\': 'backslash',
         ';': 'semicolon', "'": 'apostrophe', ',': 'comma', '.': 'dot', '/': 'slash', ' ': 'spc', '\n': 'ret', '\t': 'tab'}
ALIAS = {'meta': 'meta_l', 'cmd': 'meta_l', 'ctrl': 'ctrl', 'alt': 'alt', 'opt': 'alt', 'shift': 'shift', 'enter': 'ret',
         'backspace': 'backspace', 'del': 'delete', 'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right', 'esc': 'esc', 'space': 'spc'}

def keys_for(ch):
    if ch.isalpha() and ch.isascii():
        return (['shift'] if ch.isupper() else []) + [ch.lower()]
    if ch.isdigit(): return [ch]
    if ch in PLAIN: return [PLAIN[ch]]
    if ch in SHIFTED:
        k = SHIFTED[ch]; return ['shift', PLAIN.get(k, k)]
    raise SystemExit(f"cannot type {ch!r}")

def press(q, names):
    ks = [{"type": "qcode", "data": ALIAS.get(n, n)} for n in names]
    q.ev([{"type": "key", "data": {"down": True, "key": k}} for k in ks]); time.sleep(0.03)
    q.ev([{"type": "key", "data": {"down": False, "key": k}} for k in reversed(ks)]); time.sleep(0.03)

def ocr_words(q):
    """The screen's words as (left, top, width, height, text, line key), read by tesseract."""
    png = os.path.join(SHOTS, ".ocr.png")
    q.cmd("screendump", filename=png, format="png")
    pw, ph = struct.unpack(">II", open(png, "rb").read(24)[16:24])
    try:
        tsv = subprocess.run(["tesseract", png, "-", "--psm", "11", "tsv"], capture_output=True, text=True,
                             check=True).stdout
    except FileNotFoundError:
        raise SystemExit("tesseract is missing from the image: macbox build, then macbox down && macbox up")
    words = []
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or not f[11].strip() or float(f[10]) < 30: continue
        l, t, w, h = (int(v) for v in f[6:10])
        # Guest pixels: the screendump may be larger than the screen clicks are scaled to (HiDPI).
        words.append((l * W / pw, t * H / ph, w * W / pw, h * H / ph, f[11], tuple(f[1:5])))
    return words

def _norm(t):
    return re.sub(r"^\W+|\W+$", "", t.lower())

def find_text(q, text):
    """Where TEXT shows, topmost first: [(x, y, matched text)]."""
    want = [_norm(t) for t in text.split() if _norm(t)]
    if not want: raise SystemExit("find: no text to look for")
    words, hits = ocr_words(q), []
    for i in range(len(words)):
        run = words[i:i + len(want)]
        if len(run) < len(want) or any(r[5] != run[0][5] for r in run): continue
        if [_norm(r[4]) for r in run] != want: continue
        l = min(r[0] for r in run); t = min(r[1] for r in run)
        r_ = max(r[0] + r[2] for r in run); b = max(r[1] + r[3] for r in run)
        hits.append((round((l + r_) / 2), round((t + b) / 2), " ".join(r[4] for r in run)))
    return sorted(hits, key=lambda h: (h[1], h[0]))

def click(q, x, y, opts):
    q.ev(absm(x, y)); time.sleep(0.05)
    b = "right" if "right" in opts else "left"
    for _ in range(2 if "double" in opts else 1):
        q.ev(btn(b, True)); time.sleep(0.06); q.ev(btn(b, False)); time.sleep(0.06)

def main():
    a = sys.argv[1:]
    if not a: print(__doc__); return
    q = QMP(); c = a[0]
    if c == "shot":
        q.cmd("screendump", filename=os.path.abspath(a[1]), format="png")
    elif c == "click":
        click(q, float(a[1]), float(a[2]), a[3:])
    elif c == "move":
        q.ev(absm(float(a[1]), float(a[2])))
    elif c == "find":
        hits = find_text(q, " ".join(a[1:]))
        for h in hits: print(*h)
        if not hits: sys.exit(1)
    elif c == "click-text":
        nth, opts, words = 1, [], []
        it = iter(a[1:])
        for w in it:
            if w == "--nth": nth = int(next(it))
            elif w in ("--right", "--double"): opts.append(w[2:])
            else: words.append(w)
        hits = find_text(q, " ".join(words))
        if len(hits) < nth: raise SystemExit(f"click-text: {' '.join(words)!r} shows {len(hits)} time(s) on the screen")
        x, y, t = hits[nth - 1]; click(q, x, y, opts); print(x, y, t)
    elif c == "wait-text":
        end = time.time() + float(a[1])
        while True:
            hits = find_text(q, " ".join(a[2:]))
            if hits or time.time() >= end: break
            time.sleep(3)
        for h in hits: print(*h)
        if not hits: print("timeout"); sys.exit(1)
    elif c == "scroll":
        q.ev(absm(float(a[1]), float(a[2])))
        n = int(a[3]); b = "wheel-up" if n < 0 else "wheel-down"
        for _ in range(abs(n)):
            q.ev(btn(b, True)); q.ev(btn(b, False)); time.sleep(0.03)
    elif c == "type":
        # "type -" reads the text from stdin, so secrets stay out of process arguments.
        text = sys.stdin.read().rstrip("\n") if a[1:] == ["-"] else " ".join(a[1:])
        for ch in text: press(q, keys_for(ch))
    elif c == "key":
        for combo in a[1:]: press(q, combo.split("-")); time.sleep(0.1)
    elif c == "wait":
        # Wait (up to N s) until the screen differs from now by more than a spinner's worth.
        tmp = os.path.join(SHOTS, ".w.ppm")
        def grab():
            q.cmd("screendump", filename=tmp)
            d = open(tmp, "rb").read(); return d[d.index(b"255\n") + 4:]
        base = grab(); end = time.time() + float(a[1]); n = 0
        while time.time() < end:
            time.sleep(3); cur = grab()
            n = sum(1 for i in range(0, len(cur), 48) if cur[i] != base[i])
            if n > len(cur) // 48 // 100: break
        print("changed" if n > len(base) // 48 // 100 else "timeout")
        if len(a) > 2: q.cmd("screendump", filename=os.path.abspath(a[2]), format="png")
    else:
        raise SystemExit(__doc__)

main()
