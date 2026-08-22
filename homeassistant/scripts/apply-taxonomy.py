#!/usr/bin/env python3
"""Apply entity renames and label assignments over the Home Assistant websocket API.

Prompt 02 tooling. Three modes, and the order is not optional:

    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=...

    ./apply-taxonomy.py dump    > plan.yaml    # 1. read the live registries
    #                                            2. edit plan.yaml by hand
    ./apply-taxonomy.py apply plan.yaml --dry-run   # 3. see exactly what would change
    ./apply-taxonomy.py apply plan.yaml            # 4. do it, writing a rollback file
    ./apply-taxonomy.py rollback rollback-....yaml # 5. undo, if it went wrong

RENAMES BREAK REFERENCES. `dump` emits a grep report of every place each
entity_id appears in this repo, so nothing is renamed out from under an
automation without it being visible first. Fix those references in the same
commit as the rename.

Only stdlib + PyYAML, so it runs anywhere the repo does.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

try:
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover - the older API is still very common
    try:
        from websockets.client import connect  # type: ignore
    except ImportError:
        sys.exit("websockets is required: pip install websockets")

REPO_ROOT = Path(__file__).resolve().parent.parent


class HA:
    """Minimal websocket client. HA's websocket API is a simple id/type protocol."""

    def __init__(self, url: str, token: str) -> None:
        self.ws_url = url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        self.token = token
        self._id = 0
        self._conn = None

    async def __aenter__(self) -> "HA":
        self._conn = await connect(self.ws_url, max_size=64 * 1024 * 1024)
        hello = json.loads(await self._conn.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting: {hello}")
        await self._conn.send(json.dumps({"type": "auth", "access_token": self.token}))
        result = json.loads(await self._conn.recv())
        if result.get("type") != "auth_ok":
            raise RuntimeError(f"authentication failed: {result}")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def cmd(self, **payload):
        self._id += 1
        payload["id"] = self._id
        await self._conn.send(json.dumps(payload))
        # Skip anything that is not the reply to this command (events etc).
        while True:
            msg = json.loads(await self._conn.recv())
            if msg.get("id") == self._id and msg.get("type") == "result":
                if not msg.get("success", False):
                    raise RuntimeError(f"{payload.get('type')} failed: {msg.get('error')}")
                return msg.get("result")


def grep_repo(entity_id: str) -> list[str]:
    """Every place this entity_id appears in the repo. Renames break these."""
    try:
        out = subprocess.run(
            ["git", "grep", "-n", "--fixed-strings", entity_id],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        return [line for line in out.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, OSError):
        return []


async def do_dump(args) -> int:
    async with HA(args.url, args.token) as ha:
        entities = await ha.cmd(type="config/entity_registry/list")
        devices = await ha.cmd(type="config/device_registry/list")
        areas = await ha.cmd(type="config/area_registry/list")
        try:
            labels = await ha.cmd(type="config/label_registry/list")
        except RuntimeError:
            labels = []  # older cores have no label registry

    area_by_id = {a["area_id"]: a["name"] for a in areas}
    device_area = {d["id"]: d.get("area_id") for d in devices}
    device_name = {d["id"]: (d.get("name_by_user") or d.get("name")) for d in devices}

    plan = {
        "_generated": datetime.now(timezone.utc).isoformat(),
        "_instructions": (
            "Edit `new_entity_id` and `add_labels` below, then run "
            "`apply-taxonomy.py apply plan.yaml --dry-run`. Leave new_entity_id "
            "identical to entity_id to skip renaming that entity. Anything with "
            "`repo_references` is referenced in committed YAML - fix those in "
            "the same commit as the rename."
        ),
        "_existing_areas": sorted(area_by_id.values()),
        "_existing_labels": sorted(l.get("name", "") for l in labels),
        "entities": [],
    }

    for e in sorted(entities, key=lambda x: (device_area.get(x.get("device_id")) or "~", x["entity_id"])):
        eid = e["entity_id"]
        area = e.get("area_id") or device_area.get(e.get("device_id"))
        refs = grep_repo(eid)
        row = {
            "entity_id": eid,
            "new_entity_id": eid,
            "friendly_name": e.get("name") or e.get("original_name") or "",
            "area": area_by_id.get(area, ""),
            "device": device_name.get(e.get("device_id"), ""),
            "platform": e.get("platform", ""),
            "disabled": bool(e.get("disabled_by")),
            "hidden": bool(e.get("hidden_by")),
            "current_labels": e.get("labels", []),
            "add_labels": [],
        }
        if refs:
            row["repo_references"] = refs
        plan["entities"].append(row)

    yaml.safe_dump(plan, sys.stdout, sort_keys=False, allow_unicode=True, width=200)

    counts: dict[str, int] = {}
    for e in entities:
        counts[e["entity_id"].split(".")[0]] = counts.get(e["entity_id"].split(".")[0], 0) + 1
    print(f"\n# {len(entities)} entities, {len(devices)} devices, {len(areas)} areas, {len(labels)} labels",
          file=sys.stderr)
    for dom, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"#   {dom:<24} {n}", file=sys.stderr)
    return 0


def load_plan(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or "entities" not in data:
        raise SystemExit(f"{path} does not look like a plan (no `entities:` key)")
    return data["entities"]


def compute_changes(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    renames, labellings = [], []
    seen_targets: dict[str, str] = {}
    for row in rows:
        old, new = row.get("entity_id"), row.get("new_entity_id")
        if not old:
            continue
        if new and new != old:
            if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", new):
                raise SystemExit(f"invalid entity_id '{new}' (domain.object_id, lowercase, no spaces)")
            if old.split(".")[0] != new.split(".")[0]:
                raise SystemExit(f"cannot change domain: {old} -> {new}")
            if new in seen_targets:
                raise SystemExit(f"two entities both rename to {new}: {seen_targets[new]} and {old}")
            seen_targets[new] = old
            renames.append({"from": old, "to": new})
        add = row.get("add_labels") or []
        if add:
            labellings.append({
                "entity_id": new or old,
                "current": row.get("current_labels") or [],
                "add": add,
            })
    return renames, labellings


async def do_apply(args) -> int:
    rows = load_plan(Path(args.plan))
    renames, labellings = compute_changes(rows)

    referenced = [r for r in rows if r.get("repo_references") and r.get("new_entity_id") != r.get("entity_id")]

    print(f"{len(renames)} rename(s), {len(labellings)} label change(s)")
    for r in renames:
        print(f"  rename  {r['from']}  ->  {r['to']}")
    for l in labellings:
        print(f"  label   {l['entity_id']}  += {', '.join(l['add'])}")

    if referenced:
        print(f"\n!! {len(referenced)} renamed entit{'y is' if len(referenced) == 1 else 'ies are'} "
              f"referenced in committed YAML:")
        for r in referenced:
            print(f"   {r['entity_id']} -> {r['new_entity_id']}")
            for ref in r["repo_references"]:
                print(f"      {ref}")
        print("   Update these in the same commit, or the automations break silently.")
        if not args.force:
            print("\nRefusing to apply. Re-run with --force once the repo is updated.")
            return 1

    if args.dry_run:
        print("\nDRY RUN - nothing was changed.")
        return 0

    if not renames and not labellings:
        print("nothing to do")
        return 0

    rollback = {"_generated": datetime.now(timezone.utc).isoformat(), "entities": []}
    applied = 0
    async with HA(args.url, args.token) as ha:
        for r in renames:
            await ha.cmd(type="config/entity_registry/update",
                         entity_id=r["from"], new_entity_id=r["to"])
            # Rollback rows are written in reverse-applicable form.
            rollback["entities"].append({"entity_id": r["to"], "new_entity_id": r["from"]})
            applied += 1
            print(f"renamed {r['from']} -> {r['to']}")
        for l in labellings:
            merged = sorted(set(l["current"]) | set(l["add"]))
            await ha.cmd(type="config/entity_registry/update",
                         entity_id=l["entity_id"], labels=merged)
            rollback["entities"].append({
                "entity_id": l["entity_id"],
                "new_entity_id": l["entity_id"],
                "current_labels": merged,
                "set_labels": l["current"],
            })
            applied += 1
            print(f"labelled {l['entity_id']}: {merged}")

    out = Path(args.rollback_file or f"rollback-{datetime.now().strftime('%Y%m%d-%H%M%S')}.yaml")
    out.write_text(yaml.safe_dump(rollback, sort_keys=False, allow_unicode=True))
    print(f"\n{applied} change(s) applied. Rollback written to {out}")
    print(f"Undo with: {sys.argv[0]} rollback {out}")
    return 0


async def do_rollback(args) -> int:
    data = yaml.safe_load(Path(args.rollback).read_text())
    rows = data.get("entities", [])
    print(f"reverting {len(rows)} change(s)")
    async with HA(args.url, args.token) as ha:
        for row in rows:
            eid, new = row["entity_id"], row.get("new_entity_id")
            if "set_labels" in row:
                await ha.cmd(type="config/entity_registry/update",
                             entity_id=eid, labels=row["set_labels"])
                print(f"labels restored on {eid}")
            if new and new != eid:
                await ha.cmd(type="config/entity_registry/update",
                             entity_id=eid, new_entity_id=new)
                print(f"renamed back {eid} -> {new}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("HA_URL", "http://homeassistant.local:8123"))
    p.add_argument("--token", default=os.environ.get("HA_TOKEN", ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("dump", help="write a plan from the live registries")

    ap = sub.add_parser("apply", help="apply a plan")
    ap.add_argument("plan")
    ap.add_argument("--dry-run", action="store_true", help="show changes, touch nothing")
    ap.add_argument("--force", action="store_true", help="apply even though repo references exist")
    ap.add_argument("--rollback-file")

    rb = sub.add_parser("rollback", help="undo a previously applied plan")
    rb.add_argument("rollback")

    args = p.parse_args()
    if not args.token:
        return int(bool(sys.stderr.write("HA_TOKEN is not set (Profile -> Security -> long-lived token)\n")))

    handler = {"dump": do_dump, "apply": do_apply, "rollback": do_rollback}[args.cmd]
    try:
        return asyncio.run(handler(args))
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
