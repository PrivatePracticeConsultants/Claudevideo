"""0.8.7 — the dashboard redesign (design handoff -> the real app) and the
updater's answer to a design-only zip."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "mrfx" / "web"


def test_updater_names_a_design_export_for_what_it_is(tmp_path):
    """The user dragged the redesign prototype's zip (index.html, app.js,
    charts.js, tokens.css, sample data, screen boards) onto "Update MRF
    Explorer". It must be refused with nothing changed — and say it is a
    design, not a build. was: "doesn't contain MRF Explorer" only."""
    from mrfx.updater import UpdateError, _members
    z = tmp_path / "mrf-explorer-redesign.zip"
    with zipfile.ZipFile(z, "w") as f:
        for name in ("index.html", "app.js", "charts.js", "tokens.css", "app.css", "data.js",
                     "MAPPING.md", "screens/overview--desktop.jpg"):
            f.writestr(name, "x")
    with zipfile.ZipFile(z) as f, pytest.raises(UpdateError, match="dashboard design"):
        _members(f)
    other = tmp_path / "photos.zip"
    with zipfile.ZipFile(other, "w") as f:
        f.writestr("a.jpg", "x")
    with zipfile.ZipFile(other) as f, pytest.raises(UpdateError, match="doesn't contain MRF Explorer"):
        _members(f)


def test_the_design_system_is_wired_into_the_real_dashboard():
    """Tokens from the handoff are the stylesheet's root; the old variable
    names alias them (so every existing rule follows); no light-theme
    override is left; the chart library loads before the app; every menu
    entry has an icon and a label; the phone tab bar has its five tabs."""
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    charts = (WEB / "charts.js").read_text(encoding="utf-8")
    assert "--card: #12151c;" in css and "--surface-1: var(--card);" in css
    assert "prefers-color-scheme" not in css
    assert "window.MRFX = {" in charts
    assert html.index('<script src="charts.js">') < html.index('<script src="app.js">')
    nav = re.search(r"<nav>.*?</nav>", html, re.S).group(0)
    buttons = re.findall(r"<button data-view=[^>]*>(.*?)</button>", nav)
    assert len(buttons) == 10 and all('<use href="#i-' in b and 'class="nl"' in b for b in buttons)
    tabs = re.findall(r'data-tab="([a-z]+)"', html)
    assert tabs == ["overview", "myrates", "negotiate", "market", "more"]
    for sym in set(re.findall(r'href="#(i-[a-z-]+)"', html)):
        assert f'id="{sym}"' in html, sym
    assert "function benchVisual(bench)" in js and "M.MIN_N" in js
    # classList.toggle(name, undefined) FLIPS: the menu's "active" must get a
    # boolean, or every entry lights up in turn on screens outside a group
    assert '!!(b.dataset.view === view || (g && b.dataset.group === g))' in js


def test_charts_never_print_a_number_for_a_thin_market():
    """The bullet for a code priced by fewer than 5 practices is the hatched
    'not enough data' bar — no band, no median, no 'you' dot (honesty
    contract). Checked in the vendored library itself (run under Node when
    available)."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = """
      global.window = global; global.document = { addEventListener(){}, documentElement: {} };
      window.matchMedia = () => ({ matches: false }); window.addEventListener = () => {};
      require(process.argv[1]);
      const M = window.MRFX;
      const thin = M.bullet({ n: 4, p10: 1, p25: 2, median: 3, p75: 4, p90: 5, you: 3 }, {});
      const ok = M.bullet({ n: 9, p10: 1, p25: 2, median: 3, p75: 4, p90: 5, you: 3.5 }, {});
      console.log(JSON.stringify({ thin, ok, min: M.MIN_N }));
    """
    out = subprocess.run([node, "-e", script, str(WEB / "charts.js")], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json
    got = json.loads(out.stdout)
    assert got["min"] == 5
    assert "thin-data" in got["thin"] and "b-you" not in got["thin"] and "b-med" not in got["thin"]
    assert "b-band" in got["ok"] and "b-med" in got["ok"] and "b-you" in got["ok"]
