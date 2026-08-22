#!/usr/bin/env python3
"""Template verification against Home Assistant's REAL Jinja environment.

Two checks, matching the two ways a bad template fails:

1. COMPILE every template in the repo. Jinja resolves statically-written
   filters and tests at compile time, so an unknown one fails here.
2. Check DYNAMICALLY-DISPATCHED names — map('x'), select('x'),
   selectattr(..., 'x') — against the environment's filter/test registries.
   These resolve at RUNTIME, so they compile fine and then throw in
   production. This is how map('extract') — an Ansible filter that does not
   exist in Jinja or HA — survived compile and would have crashed the
   notification router on its first real push.

Run with a python that has the pinned homeassistant installed:
    HA_PYTHON=.venv/bin/python  ->  $HA_PYTHON scripts/check-templates.py
scripts/validate.sh runs this automatically when HA_PYTHON is usable.
"""
from __future__ import annotations
import re, sys, pathlib
from unittest.mock import MagicMock

import yaml
from jinja2 import TemplateSyntaxError, TemplateAssertionError
import homeassistant.helpers.template as ht

ROOT = pathlib.Path(__file__).resolve().parent.parent
# MagicMock hass: registers the full filter/test/global set (a hass-less env
# omits everything hass-bound and would false-positive on states/is_state/...).
ENV = ht.TemplateEnvironment(MagicMock())

class HAL(yaml.SafeLoader):
    pass
for _t in ('!secret', '!include', '!include_dir_named', '!include_dir_merge_list',
           '!include_dir_merge_named', '!include_dir_list', '!env_var', '!input'):
    HAL.add_constructor(_t, lambda l, n: "PLACEHOLDER")

def walk(node, path=''):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")
    elif isinstance(node, str) and ('{{' in node or '{%' in node):
        yield path.lstrip('.'), node

FILTER_DISPATCH = re.compile(r"\bmap\(\s*'([a-z_]+)'")
TEST_DISPATCH = re.compile(r"\b(?:select|reject)\(\s*'([a-z_]+)'")
ATTR_TEST_DISPATCH = re.compile(r"\b(?:selectattr|rejectattr)\(\s*'[^']+'\s*,\s*'([a-z_]+)'")

def main() -> int:
    files = sorted(
        list(ROOT.glob('packages/*.yaml')) + list(ROOT.glob('optional/*.yaml'))
        + list(ROOT.glob('dashboards/*.yaml'))
        + list(ROOT.glob('blueprints/automation/local/*.yaml')))
    total, problems = 0, []

    for f in files:
        try:
            doc = yaml.load(f.read_text(), Loader=HAL)
        except yaml.YAMLError as e:
            problems.append(f"{f.name}: unparseable YAML: {e}")
            continue
        for path, tpl in walk(doc):
            total += 1
            try:
                ENV.compile(tpl)
            except (TemplateSyntaxError, TemplateAssertionError) as e:
                problems.append(f"{f.name} @ {path}: {type(e).__name__}: {e}")
            for name in FILTER_DISPATCH.findall(tpl):
                if name not in ENV.filters:
                    problems.append(f"{f.name} @ {path}: map('{name}') — no such "
                                    f"filter; this compiles but throws at runtime")
            for name in TEST_DISPATCH.findall(tpl) + ATTR_TEST_DISPATCH.findall(tpl):
                if name not in ENV.tests:
                    problems.append(f"{f.name} @ {path}: dispatched test '{name}' "
                                    f"does not exist; compiles but throws at runtime")

    print(f"template check: {total} templates against HA "
          f"{__import__('homeassistant.const', fromlist=['__version__']).__version__}")
    if problems:
        print(f"{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  {p}")
        return 1
    print("all templates compile, all dispatched filter/test names exist")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
