#!/usr/bin/env python3
"""Structural audit of this repo. The offline half of docs/prompts/14-audit.md.

Finds what check_config cannot: references to helpers nothing declares,
duplicate ids that HA would silently collapse, helper name collisions between
packages (packages merge, so a clash breaks one of them), automations missing
the global kill switch, and hardcoded entity targets that should be labels.

    ./scripts/audit.py          # exits non-zero if anything is wrong

The half that needs the live instance (never-fired automations, orphaned
registry entries, database growth) is in docs/prompts/14-audit.md.
"""
from __future__ import annotations
import collections, re, sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent.parent

class HAL(yaml.SafeLoader):
    pass
for _t in ('!secret', '!include', '!include_dir_named', '!include_dir_merge_list',
           '!include_dir_merge_named', '!include_dir_list', '!env_var', '!input'):
    HAL.add_constructor(_t, lambda l, n: f"<{n.tag}>")

HELPER_DOMAINS = ('input_boolean', 'input_number', 'input_select',
                  'input_datetime', 'input_text', 'timer')
# Entities in these domains can only come from this repo, so a reference to an
# undeclared one is a real dangling reference, not an integration-provided entity.
MUST_DECLARE = HELPER_DOMAINS + ('script', 'alarm_control_panel')
# Referenced on purpose, created by config the user enables. Documented in
# docs/decisions.md; each has a defined fallback when absent.
KNOWN_EXTERNAL = {'sensor.monthly_energy'}

ENTITY_RE = re.compile(
    r"\b((?:sensor|binary_sensor|input_boolean|input_number|input_select|"
    r"input_datetime|input_text|timer|script|alarm_control_panel)\.[a-z0-9_]+)")
SERVICE_RE = re.compile(r"^\s*-?\s*(?:action|service):\s*")
ACTS_ON_HOUSE = ('light.turn', 'switch.turn', 'lock.lock', 'climate.set',
                 'fan.turn', 'homeassistant.turn')

failures: list[str] = []

def report(title: str, problems: list[str], ok_msg: str) -> None:
    print(f"\n== {title} ==")
    if problems:
        failures.extend(problems)
        for p in problems:
            print(f"   {p}")
    else:
        print(f"   {ok_msg}")

def load(p: Path):
    try:
        return yaml.load(p.read_text(), Loader=HAL)
    except yaml.YAMLError as e:
        failures.append(f"{p}: unparseable: {e}")
        return None

def slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

def declared_entities(files) -> tuple[set[str], list[str]]:
    """Everything this repo creates, plus any cross-package name collisions."""
    found, owner, clashes = set(), {}, []
    for p in files:
        d = load(p)
        if not isinstance(d, dict):
            continue
        for dom in HELPER_DOMAINS + ('script',):
            for key in (d.get(dom) or {}):
                eid = f"{dom}.{key}"
                found.add(eid)
                if eid in owner:
                    clashes.append(f"{eid} declared in both {owner[eid]} and {p.name}")
                owner[eid] = p.name
        for block in (d.get('template') or []):
            for kind in ('sensor', 'binary_sensor'):
                for item in (block.get(kind) or []):
                    if item.get('name'):
                        found.add(f"{kind}.{slugify(item['name'])}")
        for item in (d.get('command_line') or []):
            for kind in ('sensor', 'binary_sensor'):
                if kind in item and item[kind].get('name'):
                    found.add(f"{kind}.{slugify(item[kind]['name'])}")
        for item in (d.get('alarm_control_panel') or []):
            if item.get('name'):
                found.add(f"alarm_control_panel.{slugify(item['name'])}")
    return found, clashes

def main() -> int:
    pkg = sorted(ROOT.glob('packages/*.yaml'))
    opt = sorted(ROOT.glob('optional/*.yaml'))
    dash = sorted(ROOT.glob('dashboards/*.yaml'))
    declared, clashes = declared_entities(pkg + opt)
    print(f"{len(pkg)} packages, {len(opt)} optional, {len(dash)} dashboards; "
          f"{len(declared)} entities declared")

    # --- dangling references ---
    refs = collections.defaultdict(list)
    for p in pkg + opt + dash:
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if line.strip().startswith('#') or SERVICE_RE.match(line):
                continue          # `action: timer.start` is a service, not an entity
            if 'entity_globs' in line:
                continue          # recorder glob patterns are not references
            for m in ENTITY_RE.findall(line):
                refs[m].append(f"{p.name}:{i}")
    dangling = [f"{e} referenced at {locs[0]} but nothing declares it"
                for e, locs in sorted(refs.items())
                if e.startswith(MUST_DECLARE) and e not in declared]
    report("dangling helper/script references", dangling, "none")

    report("helper name collisions between packages", clashes, "none")

    # --- duplicate ids ---
    aut_ids, uids, autos = collections.Counter(), collections.Counter(), []
    for p in pkg + opt:
        d = load(p) or {}
        for a in (d.get('automation') or []):
            autos.append((p.name, a))
            if a.get('id'):
                aut_ids[a['id']] += 1
        for m in re.findall(r'unique_id:\s*([a-z0-9_]+)', p.read_text()):
            uids[m] += 1
    report("duplicate automation ids",
           [f"{k} appears {v}x" for k, v in aut_ids.items() if v > 1],
           f"{len(autos)} automations, all ids unique")
    report("duplicate unique_ids",
           [f"{k} appears {v}x" for k, v in uids.items() if v > 1],
           f"{sum(uids.values())} unique_ids, all distinct")

    # --- required fields ---
    report("automations missing required fields",
           [f"{f}: {a.get('id', '?')} missing {m}" for f, a in autos
            if (m := [x for x in ('id', 'alias', 'description', 'mode') if not a.get(x)])],
           "all have id, alias, description and mode")

    # --- kill switch ---
    # An automation may be exempt from the kill switch (an alarm response, a
    # battery-safety interlock), but the exemption must be written down.
    sources = {f.name: f.read_text() for f in pkg + opt}
    missing_kill = []
    for fname, a in autos:
        conds = yaml.dump(a.get('conditions') or [])
        acts = yaml.dump(a.get('actions') or [])
        if any(k in acts for k in ACTS_ON_HOUSE) and 'automations_paused' not in conds:
            body = (a.get('description') or '') + sources.get(fname, '')
            if 'DELIBERATELY EXEMPT' not in body:
                missing_kill.append(f"{fname}: {a.get('id')} acts on the house "
                                    f"without checking automations_paused, and the "
                                    f"exemption is not documented")
    report("kill-switch coverage", missing_kill,
           "every acting automation checks it, or documents why it must not")

    # --- hardcoded targets (the pack's tech-debt check) ---
    hard = []
    for p in pkg:
        for i, line in enumerate(p.read_text().splitlines(), 1):
            s = line.strip()
            if s.startswith('#') or '{{' in line or '{%' in line:
                continue
            m = re.match(r"entity_id:\s*((?:light|switch|lock|fan|climate|media_player)"
                         r"\.[a-z0-9_]+)", s)
            if m:
                hard.append(f"{p.name}:{i} targets {m.group(1)} directly — use a label or area")
    report("hardcoded entity targets", hard, "none — everything targets labels or areas")

    # --- secrets documented ---
    used = set()
    for p in ROOT.rglob('*.yaml'):
        if p.name == 'secrets.yaml' or '.venv' in str(p):
            continue
        used |= set(re.findall(r'!secret\s+([a-z0-9_]+)', p.read_text()))
    documented = set()
    for ex in (ROOT / 'secrets.yaml.example', ROOT / 'esphome/secrets.yaml.example'):
        if ex.exists():
            documented |= set(re.findall(r'^([a-z0-9_]+):', ex.read_text(), re.M))
    report("!secret keys missing from the examples",
           [f"{k} is used but not documented" for k in sorted(used - documented)],
           f"all {len(used)} documented")

    print()
    if failures:
        print(f"\033[31mAUDIT FAILED: {len(failures)} problem(s)\033[0m")
        return 1
    print("\033[32mAUDIT CLEAN\033[0m")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
