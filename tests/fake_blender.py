"""Stand-in for `blender -b file -s S -e E -o OUT -a` in queue tests.

Prints Blender 5.x style lines, takes FRAME_S seconds per frame and APPENDS
one "x" to each frame's file per render - so a file with "xx" means the
frame was rendered twice. With --oom-at N (first attempt only, i.e. when the
run started before N) it prints a real Cycles out-of-memory line on frame N,
then - like a real Blender that is not stopped - carries on and saves a
broken frame 5 s later. The watcher must kill it before that.
"""
import sys
import time

a = sys.argv
s = int(a[a.index("-s") + 1])
e = int(a[a.index("-e") + 1])
out = a[a.index("-o") + 1]
oom_at = int(a[a.index("--oom-at") + 1]) if "--oom-at" in a else 0
frame_s = float(a[a.index("--frame-s") + 1]) if "--frame-s" in a else 0.6


def say(text):
    print(text, flush=True)


say('00:00.100  blend            | Read blend: "fake.blend"')
say("00:00.200  render           | Rendering animation (frames %d..%d)" % (s, e))
for f in range(s, e + 1):
    say("00:00.300  render           | Rendering frame %d" % f)
    if oom_at and f == oom_at and s < oom_at:
        say("00:00.400  cycles           | ERROR CUDA error: Out of memory in "
            "cuMemAlloc(&device_pointer, size), line 1234")
        time.sleep(5)
        open("%s%04d.BROKEN" % (out, f), "w").close()
    time.sleep(frame_s)
    with open("%s%04d.png" % (out, f), "a") as fh:
        fh.write("x")
    say("00:00.500  render           | Saved: '%s%04d.png'" % (out, f))
    say("00:00.500  render           | Time: 00:00.60 (Saving: 00:00.00)")
say("Blender quit")
