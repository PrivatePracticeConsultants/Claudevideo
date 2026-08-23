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
