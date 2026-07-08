#!/usr/bin/env python3
"""CLI orchestrator: resolve NPIs -> discover TOCs -> extract -> Parquet -> summary.

    python run.py --config config/targets.yaml
    python run.py --config config/targets.yaml --stage npi        # just the NPI set
    python run.py --config config/targets.yaml --limit-files 3    # smoke test
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from src.config import load_config
from src.extractor import Extractor
from src.http_util import make_client
from src.npi_resolver import load_targets, resolve_targets, save_targets
from src.toc import discover_all
from src.writer import RecordWriter

console = Console()


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def stage_npi(client, cfg, force: bool):
    target_path = cfg.paths.raw_dir / "target_npis.parquet"
    if target_path.exists() and not force:
        console.print(f"[dim]reusing cached target set {target_path} (use --refresh-npis to rebuild)[/dim]")
        return load_targets(cfg.paths.raw_dir)
    targets = resolve_targets(client, cfg)
    path = save_targets(targets, cfg.paths.raw_dir)
    console.print(f"target NPI set saved -> [bold]{path}[/bold] ({len(targets.providers)} orgs)")
    return targets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/targets.yaml")
    ap.add_argument("--stage", choices=["all", "npi", "toc", "extract"], default="all")
    ap.add_argument("--limit-files", type=int, default=None, help="extract at most N in-network files (smoke tests)")
    ap.add_argument("--refresh-npis", action="store_true", help="ignore cached target_npis.parquet")
    ap.add_argument("--retry-failed", action="store_true", help="retry files checkpointed as failed")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    cfg.paths.raw_dir.mkdir(parents=True, exist_ok=True)

    with make_client(cfg.http) as client:
        targets = stage_npi(client, cfg, force=args.refresh_npis)
        _print_target_sample(targets)
        if args.stage == "npi":
            return 0

        sources = discover_all(client, cfg)
        console.print(f"discovered [bold]{len(sources)}[/bold] unique in-network files "
                      f"({sum(1 for s in sources if s.payer == 'blue_kc')} blue_kc, "
                      f"{sum(1 for s in sources if s.payer == 'anthem_mo')} anthem_mo)")
        _dump_manifest(cfg.paths.raw_dir, sources)
        if args.stage == "toc":
            return 0

        if args.limit_files:
            sources = sources[: args.limit_files]
            console.print(f"[yellow]--limit-files: extracting only {len(sources)} files[/yellow]")

        writer = RecordWriter(cfg.paths.out_dir)
        extractor = Extractor(cfg=cfg, targets=targets, client=client, writer=writer)
        summary = extractor.extract_all(sources, retry_failed=args.retry_failed)

    console.rule("run summary")
    for k, v in summary.items():
        console.print(f"  {k:>20}: {v}")
    console.print(f"\nquery it:  .venv/bin/python -m src.query {cfg.paths.out_dir} rate-distribution")
    return 0


def _print_target_sample(targets) -> None:
    t = Table(title=f"target NPI set — {len(targets.providers)} organizations (first 15)")
    for col in ("npi", "tin", "org_name"):
        t.add_column(col)
    for p in targets.providers[:15]:
        t.add_row(p.npi, p.tin or "-", p.org_name[:60])
    console.print(t)


def _dump_manifest(raw_dir: Path, sources) -> None:
    manifest = raw_dir / "source_manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {"payer": s.payer, "url": s.url, "plan_name": s.plan_name, "plan_id": s.plan_id}
                for s in sources
            ],
            indent=1,
        )
    )
    console.print(f"[dim]source manifest -> {manifest}[/dim]")


if __name__ == "__main__":
    sys.exit(main())
