import json, time
from playwright.sync_api import sync_playwright
BASE = "http://localhost:8791"
SHOTS = __import__("pathlib").Path(__file__).parent / "shots"

class Rec:
    def __init__(self, page):
        self.console, self.pageerr, self.failed, self.bad = [], [], [], []
        page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.pageerr.append(str(e)))
        page.on("requestfailed", lambda r: self.failed.append(f"{r.method} {r.url[:120]} {r.failure}"))
        page.on("response", lambda r: self.bad.append(f"{r.status} {r.request.method} {r.url[:140]}") if r.status >= 400 else None)
    def dump(self, tag=""):
        print(f"--- {tag} console:", self.console[-15:])
        print(f"--- {tag} pageerrors:", self.pageerr[-10:])
        print(f"--- {tag} requestfailed:", self.failed[-15:])
        print(f"--- {tag} http>=400:", self.bad[-25:])

def launch(pw):
    b = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True)
    return b, ctx
