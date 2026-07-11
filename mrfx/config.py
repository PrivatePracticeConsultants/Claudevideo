"""MRF Explorer configuration (config/mrfx.yaml), pydantic-validated."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from .catalog import DEFAULT_CODE_SET

DOLLAR_TYPES = ("negotiated", "fee schedule", "derived")


class ConfigFileError(Exception):
    """config/mrfx.yaml is unreadable or invalid — message tells the user
    exactly what to fix. Raised instead of a raw traceback so a typo in the
    one file every user edits never kills the program cryptically."""


class CodesConfig(BaseModel):
    all_codes: bool = False
    cpt_codes: list[str] = Field(default_factory=lambda: list(DEFAULT_CODE_SET))


class EnrichmentConfig(BaseModel):
    # Literal so a typo ("bulck") is a clear config error instead of silently
    # falling through to API mode and hammering NPPES
    mode: Literal["api", "bulk", "off"] = "api"
    bulk_csv_path: Path | None = None


class ReportBranding(BaseModel):
    name: str = "MRF Explorer"
    logo_path: Path | None = None


class MrfxConfig(BaseModel):
    codes: CodesConfig = Field(default_factory=CodesConfig)
    # RESERVED — accepted for forward compatibility but NOT read by mrfx
    # yet (the legacy src/ pipeline uses its own copies). Setting these does
    # not filter anything today.
    # optional targeting: NPIs from NPPES discovery, TINs from remits/contracts
    target_npis: list[str] = Field(default_factory=list)
    target_tins: list[str] = Field(default_factory=list)
    entity_map_path: Path = Path("config/entity_map.yaml")
    # entity | tin | npi; unset resolves to entity when an entity map exists, else tin
    default_grain: str | None = None
    mpfs_path: Path | None = None  # optional CMS MPFS extract (code, locality, non_facility_rate)
    report_branding: ReportBranding = Field(default_factory=ReportBranding)
    payer_name_map: dict[str, str] = Field(default_factory=dict)
    inbox_dir: Path = Path("data/inbox")
    processed_dir: Path = Path("data/processed")
    failed_dir: Path = Path("data/failed")
    store_dir: Path = Path("data/mrfx_store")
    move_processed: bool = True
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    port: int = Field(default=8377, ge=1, le=65535)
    confirm_over_gb: float = Field(default=5.0, gt=0)
    # URL-drop ingestion: staging for downloads, and knobs for aggregating many
    # files without filling the disk.
    downloads_dir: Path = Path("data/downloads")
    delete_raw_after_ingest: bool = True   # keep only the compact Parquet (re-downloadable)
    # queue workers parsing files at the same time (each is a separate CPU
    # process; the database is only ever written by the main process). 1 = one
    # file at a time; 2-3 roughly doubles/triples throughput on 4+ cores.
    # parser worker processes. 0 = auto: use (CPU cores - 1), capped at 8 —
    # the safe default that scales to the machine. A positive value pins it
    # (still clamped to cores-1). More workers = faster grinds on multi-core;
    # never changes per-file behavior, so it can't affect the error rate.
    parallel_ingests: int = Field(default=0, ge=0)
    max_toc_files: int = Field(default=2000, ge=1)  # cap child files enqueued from one TOC
    # JavaScript-only portals: when a pasted page yields no static links,
    # render it in headless Chromium and harvest links from the rendered DOM
    # and the page's own API responses. Needs the optional Playwright install
    # (pip install playwright && playwright install chromium); silently
    # skipped when unavailable.
    render_js: bool = True
    download_timeout_seconds: float = Field(default=900.0, gt=0)
    download_retries: int = Field(default=4, ge=0)
    registry_path: Path = Path("config/payer_registry.yaml")
    registry_overrides_path: Path = Path("config/registry_overrides.yaml")
    known_sources_path: Path = Path("config/known_sources.yaml")
    user_agent: str = "mrf-explorer/0.1 (local analysis tool)"

    @property
    def code_set(self) -> frozenset[str] | None:
        """None means ingest ALL billing codes. Canonicalized the same way the
        parser cleans incoming billing_code values, so '97110' matches 97110,
        97110.0, or ' g0283 '."""
        if self.codes.all_codes:
            return None
        return frozenset(str(c).strip().upper().removesuffix(".0") for c in self.codes.cpt_codes)

    def normalize_payer(self, reporting_entity_name: str) -> str:
        name = (reporting_entity_name or "").strip() or "Unknown payer"
        for pattern, label in self.payer_name_map.items():
            if pattern.lower() in name.lower():
                return label
        return name

    def ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.processed_dir, self.failed_dir,
                  self.store_dir, self.downloads_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_mrfx_config(path: str | Path = "config/mrfx.yaml") -> MrfxConfig:
    p = Path(path)
    if not p.exists():
        import logging

        logging.getLogger(__name__).warning(
            "config file %s not found — running with built-in defaults "
            "(store: data/mrfx_store, inbox: data/inbox)", p
        )
        return MrfxConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8", errors="replace")) or {}
    except yaml.YAMLError as e:
        raise ConfigFileError(
            f"{p} is not valid YAML: {e}\nFix the file (or delete it to run "
            "with defaults) and re-run."
        ) from e
    except OSError as e:
        raise ConfigFileError(f"could not read {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigFileError(
            f"{p} must be a mapping of settings (key: value), not "
            f"{type(raw).__name__}. Fix the file (or delete it to run with "
            "defaults) and re-run."
        )
    try:
        cfg = MrfxConfig.model_validate(raw)
    except ValidationError as e:
        lines = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise ConfigFileError(f"{p} has invalid settings — {lines}") from e
    unknown = set(raw) - set(MrfxConfig.model_fields)
    if unknown:
        import logging

        logging.getLogger(__name__).warning(
            "%s: unknown setting(s) ignored: %s — check for typos", p,
            ", ".join(sorted(unknown)))
    return cfg
