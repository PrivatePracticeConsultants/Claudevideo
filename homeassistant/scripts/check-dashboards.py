#!/usr/bin/env python3
"""Validate YAML dashboards against the REAL frontend that ships with the
pinned Home Assistant version.

check_config does not look at Lovelace YAML at all. Nothing else in this repo
did either, which is how a `strategy: type: areas` that exists only at
dashboard level shipped inside a *view* and rendered "Unknown strategy".

Card, badge, tile-feature, view and strategy names are extracted from the
installed hass_frontend bundle at runtime, so this tracks .ha-version
automatically instead of hardcoding a list that silently goes stale.

    HA_PYTHON=.venv/bin/python -> $HA_PYTHON scripts/check-dashboards.py
"""
from __future__ import annotations
import glob, pathlib, re, sys
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    import hass_frontend
except ImportError:
    sys.exit("hass_frontend not importable — run with the HA python (HA_PYTHON)")


def frontend_registry() -> dict[str, set[str]]:
    """Scrape the shipped bundle for the names the frontend will actually accept."""
    js = glob.glob(str(pathlib.Path(hass_frontend.where()) / "frontend_latest" / "*.js"))
    cards: set[str] = set()
    badges: set[str] = set()
    features: set[str] = set()
    views: set[str] = set()
    strategies: dict[str, set[str]] = {"dashboard": set(), "view": set()}
    css_vars: set[str] = set()
    for f in js:
        s = pathlib.Path(f).read_text(encoding="utf-8", errors="replace")
        css_vars |= set(re.findall(r"var\(--([a-z0-9-]+)", s))
        cards |= set(re.findall(r'"hui-([a-z0-9-]+)-card"', s))
        badges |= set(re.findall(r'"hui-([a-z0-9-]+)-badge"', s))
        features |= set(re.findall(r'"hui-([a-z0-9-]+)-card-feature"', s))
        views |= set(re.findall(r'"hui-([a-z0-9-]+)-view"', s))
        # the built-in strategy registry: {dashboard:{name:()=>...},view:{...}}
        if 'dashboard:{"original-states"' in s:
            i = s.index('dashboard:{"original-states"')
            chunk = s[i:i + 3000]
            vi = chunk.index("view:{")
            grab = lambda t: {a or b for a, b in
                              re.findall(r'(?:(\w+)|"([\w-]+)"):\(\)=>', t)}
            strategies["dashboard"] |= grab(chunk[:vi])
            strategies["view"] |= grab(chunk[vi:vi + 1500])
    # dialog/error/internal elements are not user-selectable types
    noise = {"dialog-create", "dialog-delete", "dialog-edit", "dialog-suggest",
             "dialog-select", "error", "empty-state", "starting", "recovery-mode",
             "error-heading", "button-heading", "entity-heading"}
    return {"css_vars": css_vars,
            "card": cards - noise, "badge": badges - noise,
            "feature": features, "view": views - noise,
            "strategy_dashboard": strategies["dashboard"],
            "strategy_view": strategies["view"]}


# Keys whose children carry a `type:` that is NOT a card type. Recursing into
# them reports a tile feature as a bogus card.
NON_CARD_KEYS = {"features", "badges", "strategy", "footer", "header",
                 "tap_action", "hold_action", "double_tap_action",
                 "state_content", "conditions"}


def walk_cards(node, path, out):
    """Yield (path, card_dict) for anything that looks like a card."""
    if isinstance(node, dict):
        if "type" in node and isinstance(node["type"], str):
            out.append((path, node))
        for k, v in node.items():
            if k in NON_CARD_KEYS:
                continue
            walk_cards(v, f"{path}.{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk_cards(v, f"{path}[{i}]", out)


# Palette names the frontend composes DYNAMICALLY as var(--${color}-color)
# (tile/badge `color:` values), so static extraction cannot see them even
# though they are real. This is a closed list, NOT a suffix wildcard: a
# blanket endswith("-color") exemption would leave three quarters of a
# typical theme unvalidated and wave through `succes-color`.
DYNAMIC_PALETTE = {
    f"{c}-color" for c in (
        "red", "pink", "purple", "deep-purple", "indigo", "blue", "light-blue",
        "cyan", "teal", "green", "light-green", "lime", "yellow", "amber",
        "orange", "deep-orange", "brown", "grey", "blue-grey", "black", "white",
    )
}


def check_themes(reg_vars: set[str], problems: list[str]) -> int:
    """Theme keys become CSS custom properties (--<key>). A typo'd key does
    nothing, silently — so validate every key against the variables the shipped
    frontend consumes, plus the closed dynamic palette list above."""
    n = 0
    for f in sorted(ROOT.glob("themes/*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError as e:
            problems.append(f"{f.name}: unparseable: {e}")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{f.name}: top level is not a mapping of themes")
            continue
        for theme_name, theme in doc.items():
            if not isinstance(theme, dict):
                problems.append(f"{f.name} [{theme_name}]: theme is not a mapping")
                continue
            flat: dict[str, str] = {}
            for k, v in theme.items():
                if k == "modes":
                    if not isinstance(v, dict):
                        problems.append(f"{f.name} [{theme_name}]: modes is not a mapping")
                        continue
                    for mode_name, mode in v.items():
                        if not isinstance(mode, dict):
                            problems.append(f"{f.name} [{theme_name}]: mode "
                                            f"'{mode_name}' is not a mapping")
                            continue
                        flat.update(mode)
                else:
                    flat[k] = v
            for key in flat:
                n += 1
                if key not in reg_vars and key not in DYNAMIC_PALETTE:
                    problems.append(f"{f.name} [{theme_name}]: '{key}' is not a "
                                    f"CSS variable the shipped frontend consumes")
    return n


def main() -> int:
    reg = frontend_registry()
    problems: list[str] = []
    files = sorted(ROOT.glob("dashboards/*.yaml"))
    n_cards = 0

    for f in files:
        try:
            doc = yaml.safe_load(f.read_text())
        except yaml.YAMLError as e:
            problems.append(f"{f.name}: unparseable: {e}")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{f.name}: top level is not a mapping")
            continue

        # dashboard-level strategy
        if "strategy" in doc:
            t = (doc["strategy"] or {}).get("type", "")
            if not t.startswith("custom:") and t not in reg["strategy_dashboard"]:
                problems.append(
                    f"{f.name}: dashboard strategy '{t}' is not built in "
                    f"(valid: {sorted(reg['strategy_dashboard'])})")

        for vi, view in enumerate(doc.get("views") or []):
            where = f"{f.name} view[{vi}]"
            vt = view.get("type")
            if vt and vt not in reg["view"]:
                problems.append(f"{where}: view type '{vt}' invalid "
                                f"(valid: {sorted(reg['view'])})")
            if "strategy" in view:
                t = (view["strategy"] or {}).get("type", "")
                if not t.startswith("custom:") and t not in reg["strategy_view"]:
                    problems.append(
                        f"{where}: '{t}' is not a VIEW strategy "
                        f"(valid view strategies: {sorted(reg['strategy_view'])}"
                        f"; '{t}' exists at dashboard level only"
                        if t in reg["strategy_dashboard"] else
                        f"{where}: view strategy '{t}' does not exist")
            for bi, badge in enumerate(view.get("badges") or []):
                if isinstance(badge, dict) and "type" in badge:
                    bt = badge["type"]
                    if not bt.startswith("custom:") and bt not in reg["badge"]:
                        problems.append(f"{where}.badges[{bi}]: badge type "
                                        f"'{bt}' invalid (valid: {sorted(reg['badge'])})")
            found: list = []
            walk_cards(view.get("sections") or [], f"{where}.sections", found)
            walk_cards(view.get("cards") or [], f"{where}.cards", found)
            for path, card in found:
                t = card["type"]
                # sections have type: grid, which is a card type too — fine
                if t.startswith("custom:"):
                    continue
                n_cards += 1
                if t not in reg["card"]:
                    problems.append(f"{path}: card type '{t}' does not exist")
                for fi, feat in enumerate(card.get("features") or []):
                    ft = (feat or {}).get("type", "")
                    if not ft.startswith("custom:") and ft not in reg["feature"]:
                        problems.append(f"{path}.features[{fi}]: tile feature "
                                        f"'{ft}' does not exist")

    n_theme = check_themes(reg["css_vars"], problems)

    print(f"dashboard check: {len(files)} dashboards, {n_cards} cards, "
          f"{n_theme} theme vars, "
          f"against the shipped frontend "
          f"({len(reg['card'])} card types, {len(reg['feature'])} tile features)")
    if problems:
        print(f"{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  {p}")
        return 1
    print("all card, badge, feature, view, strategy and theme-variable names exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
