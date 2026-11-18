"""Every panel writes to its own box.

The Clients panel and the Leads tab's "Closures nearby" both used id="cl-out"
(and both prefixed their controls cl-). `$("#cl-out")` returns the FIRST
element with that id, so clicking "Find closures" on the Leads tab rendered the
closure list into the Clients digest on another tab — the Leads tab showed
nothing and the user's client digest was overwritten. Nothing caught it: each
half worked on its own."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "mrfx" / "web"


def test_no_id_is_used_twice_on_the_page():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    dup = sorted(k for k, n in Counter(re.findall(r'\bid="([^"]+)"', html)).items() if n > 1)
    assert dup == [], f"ids used more than once: {dup}"


def test_every_id_the_script_addresses_exists():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html)) | set(re.findall(r'\bid="([^"$]+)"', js))
    refs = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)', js)) | set(
        re.findall(r'getElementById\("([^"]+)"', js))
    assert sorted(refs - ids) == []


def test_closures_live_on_the_leads_tab():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    heading = html.index("Closures nearby")
    nxt = html.index("<h3>", heading + 1)
    box = html[heading:nxt]
    assert 'id="cz-run"' in box and 'id="cz-out"' in box
    assert 'id="cl-out"' in html[:heading]           # the Clients digest keeps its own
