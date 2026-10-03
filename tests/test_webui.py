"""Checks for webui.py file-name handling - plain Python.

    python tests/test_webui.py
"""
import glob
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = (sorted(glob.glob(os.path.join(HERE, "..", "V*")), key=lambda p: [int(x) for x in os.path.basename(p)[1:].split(".") if x.isdigit()]) or [os.path.join(HERE, "..", "cli_render_launcher")])[-1]
sys.path.insert(0, SRC)

import webui as W  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print("  %s  %-54s %r" % ("PASS" if ok else "FAIL", label, got))
    if not ok:
        FAILS.append(label)
        print("        want %r" % (want,))


print("Frame number in a file name")
check("frame at the end", W.frame_of("base0900.jpg", "base"), 900)
check("digits inside the stem ignored", W.frame_of("shot2_0481.png", "shot2_"), 481)
check("'####beauty' puts the frame first (5.2.2)", W.frame_of("0905beauty.exr", ""), 905)
check("no digits: no frame", W.frame_of("beauty.exr", ""), None)

print("\nAn empty stem lists the node's whole folder")
tmp = tempfile.mkdtemp()
try:
    for name in ("0905beauty.exr", "0906beauty.exr"):
        open(os.path.join(tmp, name), "wb").close()
    check("both files found", sorted(W._scan(os.path.join(tmp, ""))), ["0905beauty.exr", "0906beauty.exr"])
finally:
    shutil.rmtree(tmp)

print("\nOpen folder (POST /api/open)")
import re  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

tmp = tempfile.mkdtemp()
try:
    out_dir = os.path.join(tmp, "renders")
    comp_dir = os.path.join(tmp, "comp")
    os.makedirs(out_dir)
    jobs_list = [{"name": "a", "cmd": ["B", "-b", "f", "-o", os.path.join(out_dir, "shot_"), "-a"],
                  "extra_outputs": [{"prefix": os.path.join(comp_dir, "base"), "label": "jpg"}]}]
    opened = []
    url, stop = W.serve(lambda: {"ok": True}, W.page(), jobs_fn=lambda: jobs_list,
                        opener=opened.append)
    page_html = urllib.request.urlopen(url).read().decode("utf-8")
    token = re.search(r"const TOKEN = '([^']+)'", page_html).group(1)

    def post(path, tok):
        req = urllib.request.Request(url + path.lstrip("/"), data=b"", method="POST",
                                     headers={"X-CLI-Token": tok} if tok is not None else {})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as exc:
            return exc.code

    check("the page carries a real token", len(token) >= 20 and token != "__TOKEN__", True)
    check("no token: refused", post("/api/open?job=0&seq=0", None), 403)
    check("wrong token: refused", post("/api/open?job=0&seq=0", "nope"), 403)
    check("right token: the job's folder is opened",
          (post("/api/open?job=0&seq=0", token), opened), (200, [out_dir]))
    check("an output not written yet: 404, nothing opened",
          (post("/api/open?job=0&seq=1", token), len(opened)), (404, 1))
    check("unknown job / output: 404",
          (post("/api/open?job=5&seq=0", token), post("/api/open?job=0&seq=9", token)), (404, 404))
    check("POST to any other route: refused", post("/api/state", token), 403)
    try:
        code = urllib.request.urlopen(url + "api/open?job=0&seq=0").status
    except urllib.error.HTTPError as exc:
        code = exc.code
    check("a GET (a plain link) opens nothing", (code, len(opened)), (404, 1))
    before = stop.idle_s()
    time.sleep(0.3)
    urllib.request.urlopen(url + "api/state").read()
    check("a state request counts as 'window open'", stop.idle_s() < before + 0.3, True)
    stop()
finally:
    shutil.rmtree(tmp)

print("\nA finished queue keeps its window while it is open (runner.linger)")
import runner  # noqa: E402


class FakeUi:
    def __init__(self, polls_for):
        self.t = 0.0
        self.polls_for = polls_for
        self.stopped_at = None

    def idle_s(self):
        # the page polls every second until `polls_for`, then the window is closed
        return 0.0 if self.t < self.polls_for else self.t - self.polls_for

    def __call__(self):
        self.stopped_at = self.t

    def sleep(self, s):
        self.t += s


ui = FakeUi(polls_for=300)
said = []
runner.linger(ui, said.append, sleep=ui.sleep, clock=lambda: ui.t)
check("served while open, stopped ~90 s after it closed",
      (300 + runner.WEB_UI_IDLE_S <= ui.stopped_at <= 300 + runner.WEB_UI_IDLE_S + 1), True)
check("and says so in the queue log", said, ["The queue window stays available until it is closed."])
ui = FakeUi(polls_for=10 ** 9)
runner.linger(ui, said.append, sleep=ui.sleep, clock=lambda: ui.t)
check("a tab left open forever: stopped after the 2 h cap",
      ui.stopped_at, runner.WEB_UI_MAX_LINGER_S)

print()
if FAILS:
    print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("All checks passed.")
