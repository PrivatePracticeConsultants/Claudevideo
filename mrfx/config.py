"""MRF Explorer configuration (config/mrfx.yaml), pydantic-validated."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# Default PT/rehab code set (same as the sibling BCBS-MO pipeline).
DEFAULT_CPT_CODES = [
    "97161", "97162", "97163", "97164",
    "97110", "97112", "97113", "97116", "97124", "97140", "97150",
    "97530", "97535", "97542", "97760",
    "97010", "97012", "97014", "97032", "97035", "G0283",
]

# Static CPT -> short description map for the shipped PT set (dashboard).
CPT_DESCRIPTIONS = {
    "97161": "PT eval, low complexity",
    "97162": "PT eval, moderate complexity",
    "97163": "PT eval, high complexity",
    "97164": "PT re-evaluation",
    "97110": "Therapeutic exercises",
    "97112": "Neuromuscular re-education",
    "97113": "Aquatic therapy w/ exercises",
    "97116": "Gait training",
    "97124": "Massage therapy",
    "97140": "Manual therapy",
    "97150": "Group therapeutic procedures",
    "97530": "Therapeutic activities",
    "97535": "Self-care/home management training",
    "97542": "Wheelchair management training",
    "97760": "Orthotic management & training",
    "97010": "Hot/cold packs",
    "97012": "Mechanical traction",
    "97014": "Electrical stimulation (unattended)",
    "97032": "Electrical stimulation (manual)",
    "97035": "Ultrasound therapy",
    "G0283": "Electrical stimulation, non-wound",
}

DOLLAR_TYPES = ("negotiated", "fee schedule", "derived")


class CodesConfig(BaseModel):
    all_codes: bool = False
    cpt_codes: list[str] = Field(default_factory=lambda: list(DEFAULT_CPT_CODES))


class EnrichmentConfig(BaseModel):
    mode: str = "api"  # api | bulk | off
    bulk_csv_path: Path | None = None


class MrfxConfig(BaseModel):
    codes: CodesConfig = Field(default_factory=CodesConfig)
    payer_name_map: dict[str, str] = Field(default_factory=dict)
    inbox_dir: Path = Path("data/inbox")
    processed_dir: Path = Path("data/processed")
    failed_dir: Path = Path("data/failed")
    store_dir: Path = Path("data/mrfx_store")
    move_processed: bool = True
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    port: int = 8377
    confirm_over_gb: float = 5.0
    registry_path: Path = Path("config/payer_registry.yaml")
    registry_overrides_path: Path = Path("config/registry_overrides.yaml")
    user_agent: str = "mrf-explorer/0.1 (local analysis tool)"

    @property
    def code_set(self) -> frozenset[str] | None:
        """None means ingest ALL billing codes."""
        if self.codes.all_codes:
            return None
        return frozenset(str(c) for c in self.codes.cpt_codes)

    def normalize_payer(self, reporting_entity_name: str) -> str:
        name = (reporting_entity_name or "").strip() or "Unknown payer"
        for pattern, label in self.payer_name_map.items():
            if pattern.lower() in name.lower():
                return label
        return name

    def ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.processed_dir, self.failed_dir, self.store_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_mrfx_config(path: str | Path = "config/mrfx.yaml") -> MrfxConfig:
    p = Path(path)
    if not p.exists():
        return MrfxConfig()
    raw = yaml.safe_load(p.read_text()) or {}
    return MrfxConfig.model_validate(raw)
