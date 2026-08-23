#!/usr/bin/env python3
"""Generate the weather hero artwork set (www/hero/*.svg).

One variant per Home Assistant weather condition family, all derived from the
same composition (sky gradients, ridgelines, left/bottom scrims for text
legibility) so the panel stays coherent as conditions change. Deterministic —
no randomness — so regenerating produces byte-identical files and a clean diff.

Run after editing:  python3 scripts/generate-hero-art.py
"""
from __future__ import annotations
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "www" / "hero"
W, H = 1280, 560

STARS = [(150, 80, 1.6, .7), (260, 150, 1.2, .5), (420, 70, 1.4, .6),
         (90, 210, 1.2, .4), (560, 130, 1.1, .5), (340, 230, 1.3, .4),
         (700, 60, 1.5, .6), (1190, 220, 1.2, .5), (880, 190, 1.1, .4)]

RIDGES = (
    '<path d="M0 430 L140 386 L300 424 L470 372 L640 418 L840 366 L1020 412 '
    'L1180 380 L1280 404 L1280 560 L0 560 Z" fill="url(#ridgeB)" opacity="0.9"/>'
    '<path d="M0 478 L180 432 L360 472 L560 424 L760 468 L960 428 L1140 462 '
    'L1280 440 L1280 560 L0 560 Z" fill="url(#ridgeA)"/>')

# Readability scrims: the live numbers sit top-left and along the bottom.
SCRIM = (
    '<linearGradient id="scrimL" x1="0" y1="0" x2="1" y2="0">'
    '<stop offset="0%" stop-color="#070910" stop-opacity="0.75"/>'
    '<stop offset="45%" stop-color="#070910" stop-opacity="0.25"/>'
    '<stop offset="100%" stop-color="#070910" stop-opacity="0"/></linearGradient>'
    '<linearGradient id="scrimB" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="55%" stop-color="#070910" stop-opacity="0"/>'
    '<stop offset="100%" stop-color="#070910" stop-opacity="0.55"/></linearGradient>')

def doc(defs: str, art: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
            f'preserveAspectRatio="xMidYMid slice">'
            f'<defs>{SCRIM}{defs}</defs>{art}'
            f'<rect width="{W}" height="{H}" fill="url(#scrimL)"/>'
            f'<rect width="{W}" height="{H}" fill="url(#scrimB)"/>'
            f'<rect width="{W}" height="{H}" fill="none" '
            f'stroke="rgba(255,255,255,0.05)"/></svg>')

def sky(top: str, bottom: str) -> str:
    return (f'<linearGradient id="base" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="{top}"/>'
            f'<stop offset="100%" stop-color="{bottom}"/></linearGradient>')

def ridge_defs(a: str, b: str) -> str:
    return (f'<linearGradient id="ridgeA" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="{a}"/><stop offset="100%" stop-color="#0d1020"/></linearGradient>'
            f'<linearGradient id="ridgeB" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="{b}"/><stop offset="100%" stop-color="#10142a"/></linearGradient>')

def glow(gid: str, cx: str, cy: str, color: str, alpha: float) -> str:
    return (f'<radialGradient id="{gid}" cx="{cx}" cy="{cy}" r="55%">'
            f'<stop offset="0%" stop-color="{color}" stop-opacity="{alpha}"/>'
            f'<stop offset="55%" stop-color="{color}" stop-opacity="{alpha / 4}"/>'
            f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/></radialGradient>')

def stars(subset=STARS) -> str:
    dots = "".join(f'<circle cx="{x}" cy="{y}" r="{r}" opacity="{o}"/>'
                   for x, y, r, o in subset)
    return f'<g fill="#dfe6ff">{dots}</g>'

def cloud(cx: int, cy: int, s: float, fill: str, op: float) -> str:
    return (f'<g fill="{fill}" opacity="{op}" transform="translate({cx} {cy}) scale({s})">'
            '<ellipse cx="0" cy="0" rx="88" ry="34"/>'
            '<ellipse cx="-52" cy="10" rx="56" ry="26"/>'
            '<ellipse cx="55" cy="8" rx="60" ry="27"/>'
            '<ellipse cx="8" cy="-20" rx="52" ry="28"/></g>')

def rain(color: str, n: int = 26, dash: str = "14 26") -> str:
    drops = "".join(
        f'<line x1="{80 + i * 46}" y1="{30 + (i * 37) % 120}" '
        f'x2="{68 + i * 46}" y2="{95 + (i * 37) % 120}"/>' for i in range(n))
    return (f'<g stroke="{color}" stroke-width="2.4" stroke-linecap="round" '
            f'opacity="0.5" stroke-dasharray="{dash}">{drops}</g>')

def snow(n: int = 30) -> str:
    flakes = "".join(
        f'<circle cx="{60 + i * 42}" cy="{40 + (i * 53) % 260}" '
        f'r="{2.2 + (i % 3) * 0.9}" opacity="{0.35 + (i % 4) * 0.12}"/>'
        for i in range(n))
    return f'<g fill="#eef3ff">{flakes}</g>'

def base_rect() -> str:
    return f'<rect width="{W}" height="{H}" fill="url(#base)"/>'

def fill_rect(gid: str) -> str:
    return f'<rect width="{W}" height="{H}" fill="url(#{gid})"/>'

VARIANTS: dict[str, str] = {}

# -- clear night: the original composition -----------------------------------
VARIANTS["clear-night"] = doc(
    sky("#0b0e16", "#0d1020") + ridge_defs("#131829", "#181f36")
    + glow("sun", "78%", "18%", "#ff9f43", 0.5) + glow("dusk", "8%", "95%", "#4b63d8", 0.35)
    + '<linearGradient id="arc" x1="0" y1="0" x2="1" y2="0">'
      '<stop offset="0%" stop-color="#ff9f43" stop-opacity="0"/>'
      '<stop offset="45%" stop-color="#ffb357" stop-opacity="0.85"/>'
      '<stop offset="100%" stop-color="#ff9f43" stop-opacity="0"/></linearGradient>',
    base_rect() + fill_rect("dusk") + fill_rect("sun")
    + '<circle cx="998" cy="104" r="46" fill="#ffb357" opacity="0.9"/>'
      '<circle cx="998" cy="104" r="72" fill="none" stroke="#ffb357" stroke-opacity="0.25" stroke-width="2"/>'
    + stars()
    + '<path d="M 60 400 Q 640 120 1220 400" fill="none" stroke="url(#arc)" '
      'stroke-width="2.5" stroke-dasharray="1 7" stroke-linecap="round"/>'
    + RIDGES)

# -- sunny: high warm sun, no stars ------------------------------------------
VARIANTS["sunny"] = doc(
    sky("#182338", "#101625") + ridge_defs("#1b2540", "#232f52")
    + glow("sun", "72%", "12%", "#ffb357", 0.75),
    base_rect() + fill_rect("sun")
    + '<circle cx="920" cy="96" r="58" fill="#ffc36e" opacity="0.95"/>'
      '<circle cx="920" cy="96" r="88" fill="none" stroke="#ffc36e" stroke-opacity="0.3" stroke-width="2"/>'
      '<circle cx="920" cy="96" r="120" fill="none" stroke="#ffc36e" stroke-opacity="0.12" stroke-width="2"/>'
    + RIDGES)

# -- partly cloudy / windy ----------------------------------------------------
VARIANTS["partly"] = doc(
    sky("#151d30", "#0f1523") + ridge_defs("#192341", "#20294a")
    + glow("sun", "76%", "14%", "#ffb357", 0.55),
    base_rect() + fill_rect("sun")
    + '<circle cx="950" cy="100" r="48" fill="#ffc36e" opacity="0.9"/>'
    + cloud(860, 150, 1.25, "#232d47", 0.95) + cloud(1090, 90, 0.9, "#2a3554", 0.9)
    + cloud(300, 110, 1.0, "#1d2740", 0.8)
    + RIDGES)

# -- cloudy -------------------------------------------------------------------
VARIANTS["cloudy"] = doc(
    sky("#131826", "#0e121d") + ridge_defs("#171e33", "#1d2540")
    + glow("hint", "70%", "10%", "#8d96f2", 0.14),
    base_rect() + fill_rect("hint")
    + cloud(260, 120, 1.3, "#222b42", 0.95) + cloud(640, 90, 1.5, "#1c2438", 0.95)
    + cloud(1020, 140, 1.2, "#262f4a", 0.9) + cloud(860, 220, 0.9, "#1a2136", 0.8)
    + RIDGES)

# -- fog ----------------------------------------------------------------------
VARIANTS["fog"] = doc(
    sky("#141822", "#10131b") + ridge_defs("#161b28", "#1b2133"),
    base_rect()
    + "".join(f'<rect x="0" y="{150 + i * 62}" width="{W}" height="26" rx="13" '
              f'fill="#aab4cf" opacity="{0.10 - i * 0.012}"/>' for i in range(5))
    + RIDGES)

# -- rain (rainy, pouring) ----------------------------------------------------
VARIANTS["rain"] = doc(
    sky("#101725", "#0c111c") + ridge_defs("#151d31", "#1a2340")
    + glow("wet", "20%", "0%", "#4b8dd8", 0.2),
    base_rect() + fill_rect("wet")
    + cloud(420, 90, 1.4, "#1b2438", 0.95) + cloud(900, 120, 1.3, "#202a44", 0.95)
    + rain("#6faee8") + RIDGES)

# -- storm (lightning, lightning-rainy, exceptional) --------------------------
VARIANTS["storm"] = doc(
    sky("#10131f", "#0b0e16") + ridge_defs("#141a2c", "#191f38")
    + glow("flash", "58%", "8%", "#ffd97a", 0.3),
    base_rect() + fill_rect("flash")
    + cloud(500, 80, 1.5, "#181f33", 0.95) + cloud(940, 110, 1.3, "#1d2440", 0.95)
    + '<path d="M 700 150 L 655 250 L 700 250 L 640 370 L 720 245 L 675 245 Z" '
      'fill="#ffd97a" opacity="0.9"/>'
    + rain("#5f7fb8", n=18) + RIDGES)

# -- snow (snowy, snowy-rainy, hail) ------------------------------------------
VARIANTS["snow"] = doc(
    sky("#151a28", "#10141f") + ridge_defs("#1a2136", "#212a48")
    + glow("cold", "75%", "10%", "#9fc0ff", 0.2),
    base_rect() + fill_rect("cold")
    + cloud(400, 95, 1.3, "#212a44", 0.9) + cloud(880, 120, 1.2, "#273153", 0.9)
    + snow() + RIDGES)


# =============================================================================
# PANEL ART — the composed cards of the wall-panel look. Same rules: pure art,
# every number overlaid live by picture-elements.
# =============================================================================

# -- glossy panel: backdrop for the forecast rows and stat grids --------------
VARIANTS["panel"] = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 780" '
    f'preserveAspectRatio="none">'
    '<defs>'
    '<linearGradient id="pb" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#171c29"/>'
    '<stop offset="100%" stop-color="#10141f"/></linearGradient>'
    '<linearGradient id="sheen" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0%" stop-color="#ffffff" stop-opacity="0.06"/>'
    '<stop offset="40%" stop-color="#ffffff" stop-opacity="0"/></linearGradient>'
    '</defs>'
    '<rect width="1280" height="780" fill="url(#pb)"/>'
    '<rect width="1280" height="780" fill="url(#sheen)"/>'
    # divider between the forecast rows and the stat grid
    '<line x1="48" y1="368" x2="1232" y2="368" stroke="rgba(255,255,255,0.07)"/>'
    '<line x1="640" y1="408" x2="640" y2="720" stroke="rgba(255,255,255,0.07)"/>'
    '</svg>')

# -- thermostat dial: decorative gradient ring, live number overlaid ----------
def _dial() -> str:
    import math
    segs = ["#40beaf", "#4fd1a5", "#8ac97a", "#f4cf5e", "#ffa94d", "#f27ba0"]
    cx, cy, r = 320, 320, 214
    start, sweep = 135.0, 270.0
    step = sweep / len(segs)
    def pt(deg):
        a = math.radians(deg)
        return cx + r * math.cos(a), cy + r * math.sin(a)
    arcs = []
    for i, col in enumerate(segs):
        a0, a1 = start + i * step, start + (i + 1) * step
        x0, y0 = pt(a0); x1, y1 = pt(a1)
        arcs.append(f'<path d="M {x0:.1f} {y0:.1f} A {r} {r} 0 0 1 {x1:.1f} {y1:.1f}" '
                    f'stroke="{col}" stroke-width="26" fill="none" stroke-linecap="round"/>')
    ticks = []
    for i in range(28):
        a = math.radians(start + i * (sweep / 27))
        x0 = cx + 172 * math.cos(a); y0 = cy + 172 * math.sin(a)
        x1 = cx + 184 * math.cos(a); y1 = cy + 184 * math.sin(a)
        ticks.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
                     f'stroke="#3a4360" stroke-width="3" stroke-linecap="round"/>')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 640">'
        '<defs>'
        '<radialGradient id="dg" cx="50%" cy="42%" r="65%">'
        '<stop offset="0%" stop-color="#1b2133"/>'
        '<stop offset="100%" stop-color="#10141f"/></radialGradient>'
        '</defs>'
        '<rect width="640" height="640" fill="none"/>'
        f'<circle cx="{cx}" cy="{cy}" r="256" fill="url(#dg)" '
        'stroke="rgba(255,255,255,0.07)"/>'
        + "".join(arcs) + "".join(ticks) +
        f'<circle cx="{cx}" cy="{cy}" r="150" fill="#0e1220" '
        'stroke="rgba(255,255,255,0.09)" stroke-width="1.5"/>'
        '</svg>')
VARIANTS["dial"] = _dial()

# -- room illustration: the left-card scene, stylised rather than fake-photo --
VARIANTS["room"] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 800" '
    'preserveAspectRatio="xMidYMid slice">'
    '<defs>'
    '<linearGradient id="wall" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#121828"/>'
    '<stop offset="100%" stop-color="#0c101c"/></linearGradient>'
    '<linearGradient id="rgb" x1="0" y1="0" x2="1" y2="0">'
    '<stop offset="0%" stop-color="#6f9bff"/><stop offset="30%" stop-color="#b18ef2"/>'
    '<stop offset="60%" stop-color="#f27ba0"/><stop offset="100%" stop-color="#ff9f43"/>'
    '</linearGradient>'
    '<linearGradient id="rgbGlow" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#8d96f2" stop-opacity="0.35"/>'
    '<stop offset="100%" stop-color="#8d96f2" stop-opacity="0"/></linearGradient>'
    '<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#182449"/>'
    '<stop offset="100%" stop-color="#2c2a55"/></linearGradient>'
    '<linearGradient id="floor" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#141a2b"/>'
    '<stop offset="100%" stop-color="#0a0d16"/></linearGradient>'
    '<linearGradient id="scrimL2" x1="0" y1="0" x2="1" y2="0">'
    '<stop offset="0%" stop-color="#070910" stop-opacity="0.7"/>'
    '<stop offset="50%" stop-color="#070910" stop-opacity="0.15"/>'
    '<stop offset="100%" stop-color="#070910" stop-opacity="0"/></linearGradient>'
    '</defs>'
    '<rect width="1280" height="800" fill="url(#wall)"/>'
    # ceiling RGB strip + downglow
    '<rect x="0" y="34" width="1280" height="10" rx="5" fill="url(#rgb)"/>'
    '<rect x="0" y="44" width="1280" height="150" fill="url(#rgbGlow)"/>'
    # window with skyline
    '<rect x="720" y="120" width="440" height="380" rx="14" fill="url(#sky)" '
    'stroke="#232c48" stroke-width="8"/>'
    '<circle cx="1080" cy="190" r="26" fill="#e8ecff" opacity="0.9"/>'
    '<g fill="#0e1430">'
    '<rect x="760" y="300" width="60" height="200"/><rect x="830" y="250" width="52" height="250"/>'
    '<rect x="892" y="330" width="66" height="170"/><rect x="968" y="270" width="56" height="230"/>'
    '<rect x="1034" y="350" width="48" height="150"/><rect x="1090" y="300" width="54" height="200"/>'
    '</g>'
    '<g fill="#ffd97a" opacity="0.8">'
    '<rect x="770" y="320" width="7" height="9"/><rect x="790" y="350" width="7" height="9"/>'
    '<rect x="840" y="270" width="7" height="9"/><rect x="858" y="310" width="7" height="9"/>'
    '<rect x="902" y="350" width="7" height="9"/><rect x="978" y="290" width="7" height="9"/>'
    '<rect x="996" y="330" width="7" height="9"/><rect x="1100" y="320" width="7" height="9"/>'
    '</g>'
    # floor
    '<rect x="0" y="560" width="1280" height="240" fill="url(#floor)"/>'
    # sofa
    '<g>'
    '<rect x="150" y="470" width="470" height="150" rx="26" fill="#232c47"/>'
    '<rect x="180" y="420" width="410" height="110" rx="24" fill="#2a3454"/>'
    '<rect x="196" y="436" width="120" height="84" rx="18" fill="#333e63"/>'
    '<rect x="326" y="436" width="120" height="84" rx="18" fill="#333e63"/>'
    '<rect x="456" y="436" width="118" height="84" rx="18" fill="#333e63"/>'
    '<rect x="140" y="450" width="44" height="170" rx="20" fill="#1d2540"/>'
    '<rect x="586" y="450" width="44" height="170" rx="20" fill="#1d2540"/>'
    '</g>'
    # floor lamp with warm glow
    '<circle cx="86" cy="360" r="60" fill="#ff9f43" opacity="0.12"/>'
    '<circle cx="86" cy="360" r="26" fill="#ffb357" opacity="0.85"/>'
    '<rect x="82" y="386" width="8" height="200" rx="4" fill="#1b2236"/>'
    # plant
    '<g fill="#2f7d5c">'
    '<ellipse cx="676" cy="520" rx="16" ry="52" transform="rotate(-18 676 520)"/>'
    '<ellipse cx="700" cy="514" rx="16" ry="58"/>'
    '<ellipse cx="724" cy="520" rx="16" ry="50" transform="rotate(18 724 520)"/>'
    '</g>'
    '<rect x="676" y="560" width="48" height="52" rx="8" fill="#232c47"/>'
    '<rect width="1280" height="800" fill="url(#scrimL2)"/>'
    '<rect width="1280" height="800" fill="none" stroke="rgba(255,255,255,0.05)"/>'
    '</svg>')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    import xml.dom.minidom
    for name, svg in VARIANTS.items():
        xml.dom.minidom.parseString(svg)          # refuse to write broken XML
        assert "а" not in svg                # the Cyrillic-hex lesson
        (OUT / f"{name}.svg").write_text(svg + "\n")
        print(f"  wrote hero/{name}.svg ({len(svg)} bytes)")


if __name__ == "__main__":
    main()
