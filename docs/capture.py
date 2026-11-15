"""Drive headless Chrome over the DevTools protocol to capture README media from the
dashboard: the hero GIF and a phone-width screenshot.

Serve the dashboard first:   python -m http.server 8765 --directory docs
Then:                        python docs/capture.py [http://localhost:8765/]
Needs Google Chrome, Pillow and websocket-client (dev only)."""

import base64
import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket
from PIL import Image

APP = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/"
OUT = Path(__file__).resolve().parent / "img"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9334

prof = tempfile.mkdtemp()
proc = subprocess.Popen(
    [CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
     "--hide-scrollbars", "--no-first-run", "--disable-gpu", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
for _ in range(50):
    try:
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
        page = next(t for t in tabs if t["type"] == "page")
        break
    except Exception:
        time.sleep(0.2)
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
_id = 0


def cdp(method, **params):
    global _id
    _id += 1
    ws.send(json.dumps({"id": _id, "method": method, "params": params}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == _id:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


def js(expr):
    r = cdp("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
    return r.get("result", {}).get("value")


def viewport(w, h, scale, mobile=False):
    cdp("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=scale, mobile=mobile)


def load():
    cdp("Page.enable")
    cdp("Page.navigate", url=APP + "?capture=" + str(time.time()))
    for _ in range(150):
        time.sleep(0.2)
        if js("document.readyState === 'complete' && !!document.body && !document.body.classList.contains('is-loading')"):
            break
    # Freeze motion so every frame is settled, then let fonts and the parity check finish.
    js("""(() => { const s = document.createElement('style');
      s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}';
      document.head.appendChild(s); Chart.defaults.animation = false; })()""")
    js("document.fonts.ready.then(() => true)")
    time.sleep(1.2)


def shot():
    data = cdp("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
    return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")


def click(selector):
    js(f"document.querySelector({json.dumps(selector)}).click(); true")
    time.sleep(0.45)


def select(selector, value):
    js(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); e.value = {json.dumps(value)};"
       f" e.dispatchEvent(new Event('change', {{bubbles: true}})); }})(); true")
    time.sleep(0.45)


def scroll_to(y):
    js(f"window.scrollTo(0,{y}); true")
    time.sleep(0.3)


def top_of(selector):
    return js(f"document.querySelector({json.dumps(selector)}).getBoundingClientRect().top + scrollY")


# ---- Hero GIF at 1x, framed on the workspace ----
viewport(1200, 760, 1)
load()
frames, durs = [], []


def frame(ms):
    frames.append(shot())
    durs.append(ms)


ws_top = top_of(".workspace")
scroll_to(max(0, ws_top - 84))
frame(2200)                                              # JFK, any rain: +1.0%, no clear effect
click('input[name="zone"][value="cp"]'); frame(1700)     # Central Park
select("#condition", "heavy_rain"); frame(1700)
select("#condition", "snow"); frame(1900)                # the one Central Park dip with an interval below zero
select("#condition", "any_rain")
click('input[name="metric"][value="avg_fare"]'); frame(1700)
click('input[name="metric"][value="trips"]')
click('input[name="zone"][value="both"]')
scroll_to(top_of("#naive") - 70); frame(2000)           # totals count days
click('input[name="station"][value="max4"]'); frame(2200)
click('input[name="station"][value="cp"]')
click('input[name="zone"][value="jfk"]')
scroll_to(max(0, ws_top - 84)); frame(600)

pal = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
pal[0].save(OUT / "demo.gif", save_all=True, append_images=pal[1:], duration=durs, loop=0, optimize=True, disposal=1)

# ---- Phone still at 375px, 2x ----
viewport(375, 812, 2, mobile=True)
load()
scroll_to(top_of("#effect") - 72)
shot().save(OUT / "phone.png", optimize=True)

ws.close()
proc.terminate()
print("frames", len(frames))
