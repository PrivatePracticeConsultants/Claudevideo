"""Frame-time DISTRIBUTION in a real browser, not the median.

"Missing frames" is a complaint about variance, and a median hides it entirely:
a run that is smooth at 40ms and a run that alternates 20ms/60ms have the same
median and feel completely different. This records every rAF delta over a long
window, after a warm-up so level-load and shader-compile costs are not counted
as stutter, and reports the shape.
"""
import http.server, socketserver, threading, functools, time, json, statistics
from playwright.sync_api import sync_playwright

ROOT = "/home/user/Claudevideo/build/web"
PORT = 8795
WARMUP_S = 12
SAMPLE_S = 25

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

RECORD = """
() => {
  window.__d = [];
  let last = performance.now();
  const tick = (t) => { window.__d.push(t - last); last = t; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}
"""

def report(label, deltas):
    if len(deltas) < 5:
        print("%-10s too few frames (%d)" % (label, len(deltas)))
        return
    d = sorted(deltas)
    med = statistics.median(d)
    p95 = d[int(len(d) * 0.95)]
    p99 = d[min(int(len(d) * 0.99), len(d) - 1)]
    # A "long" frame is one that took over twice the typical frame. That is the
    # thing a player perceives as a hitch rather than as a low frame rate.
    longs = [x for x in deltas if x > med * 2.0]
    # Consecutive-frame variation: smoothness is about how much each frame
    # differs from the one BEFORE it, not about the spread over a minute.
    steps = [abs(deltas[i] - deltas[i - 1]) for i in range(1, len(deltas))]
    print("%-10s n=%4d  med %6.1f  p95 %6.1f  p99 %6.1f  max %6.1f  "
          "long>2x %3d (%4.1f%%)  median jitter %5.1f ms (%4.1f%%)"
          % (label, len(d), med, p95, p99, d[-1], len(longs),
             100.0 * len(longs) / len(d),
             statistics.median(steps), 100.0 * statistics.median(steps) / med))

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
        args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto("http://127.0.0.1:%d/index.html" % PORT, wait_until="load", timeout=60000)

    deadline = time.time() + 150
    while time.time() < deadline:
        w = page.evaluate("() => { const c=document.getElementById('canvas'); return c?c.width:0; }")
        if w and w > 16:
            break
        time.sleep(1)
    page.click("canvas")
    time.sleep(2)

    for label, presses in [("BALANCED", 0), ("FAST", 1), ("HIGH", 1)]:
        for _ in range(presses):
            page.keyboard.press("F2")
        time.sleep(WARMUP_S)
        page.evaluate(RECORD)
        time.sleep(SAMPLE_S)
        report(label, page.evaluate("() => window.__d.slice(2)"))

    print("page errors:", errs if errs else "none")
    browser.close()
httpd.shutdown()
