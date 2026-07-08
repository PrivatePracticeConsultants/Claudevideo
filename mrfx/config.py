"""MRF Explorer configuration (config/mrfx.yaml), pydantic-validated."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .catalog import DEFAULT_CODE_SET

DOLLAR_TYPES = ("negotiated", "fee schedule", "derived")


class CodesConfig(BaseModel):
    all_codes: bool = False
    cpt_codes: list[str] = Field(default_factory=lambda: list(DEFAULT_CODE_SET))


class EnrichmentConfig(BaseModel):
    mode: str = "api"  # api | bulk | off
    bulk_csv_path: Path | None = None


class ReportBranding(BaseModel):
    name: str = "MRF Explorer"
    logo_path: Path | None = None


class MrfxConfig(BaseModel):
    codes: CodesConfig = Field(default_factory=CodesConfig)
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
    port: int = 8377
    confirm_over_gb: float = 5.0
    # URL-drop ingestion: staging for downloads, and knobs for aggregating many
    # files without filling the disk.
    downloads_dir: Path = Path("data/downloads")
    delete_raw_after_ingest: bool = True   # keep only the compact Parquet (re-downloadable)
    max_toc_files: int = 2000              # cap child files enqueued from one TOC
    download_timeout_seconds: float = 900.0
    download_retries: int = 4
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
    raw = yaml.safe_load(p.read_text()) or {}
    return MrfxConfig.model_validate(raw)
