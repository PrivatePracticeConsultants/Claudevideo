"""Pydantic models for config/targets.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class BlueKCConfig(BaseModel):
    enabled: bool = True
    hub_url: str = "https://bcbskc.sapphiremrfhub.com"
    toc_name_filters: list[str] = Field(default_factory=list)


class EmployerGroup(BaseModel):
    ein: str | None = None
    name: str | None = None

    @field_validator("ein")
    @classmethod
    def normalize_ein(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = re.sub(r"\D", "", v)
        if len(digits) != 9:
            raise ValueError(f"EIN must have 9 digits, got {v!r}")
        return digits

    @model_validator(mode="after")
    def ein_or_name(self) -> "EmployerGroup":
        if not self.ein and not self.name:
            raise ValueError("employer group needs an `ein` or a `name`")
        return self

    def label(self) -> str:
        return self.name or self.ein or "?"


class AnthemConfig(BaseModel):
    enabled: bool = True
    s3_primary: str = (
        "https://antm-pt-prod-dataz-nogbd-nophi-us-east1.s3.amazonaws.com/"
    )
    s3_secondary: str = (
        "https://antm-pt-prod-dataz-nogbd-nophi-us-east2.s3.us-east-2.amazonaws.com/"
    )
    employer_groups: list[EmployerGroup] = Field(default_factory=list)
    file_include_patterns: list[str] = Field(
        default_factory=lambda: ["MO_", "anthembcbsmo"]
    )
    include_out_of_area_files: bool = True


class PayersConfig(BaseModel):
    blue_kc: BlueKCConfig = Field(default_factory=BlueKCConfig)
    anthem_mo: AnthemConfig = Field(default_factory=AnthemConfig)


class TaxonomyTarget(BaseModel):
    code: str | None = None
    code_prefix: str | None = None
    search_description: str

    @model_validator(mode="after")
    def code_or_prefix(self) -> "TaxonomyTarget":
        if not self.code and not self.code_prefix:
            raise ValueError("taxonomy target needs `code` or `code_prefix`")
        return self

    def matches(self, taxonomy_code: str) -> bool:
        if self.code and taxonomy_code == self.code:
            return True
        if self.code_prefix and taxonomy_code.startswith(self.code_prefix):
            return True
        return False


class ExplicitProvider(BaseModel):
    npi: str
    org_name: str | None = None
    tin: str | None = None

    @field_validator("npi")
    @classmethod
    def npi_ten_digits(cls, v: str) -> str:
        v = str(v).strip()
        if not re.fullmatch(r"\d{10}", v):
            raise ValueError(f"NPI must be 10 digits, got {v!r}")
        return v

    @field_validator("tin")
    @classmethod
    def normalize_tin(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return re.sub(r"\D", "", str(v))


class NpiTargetsConfig(BaseModel):
    state: str = "MO"
    taxonomies: list[TaxonomyTarget]
    cities: list[str] = Field(default_factory=list)
    explicit_providers: list[ExplicitProvider] = Field(default_factory=list)
    # Also resolve individual practitioners (NPI-1). Off by default (the spec
    # targets organizations), but Blue KC provider_references enumerate
    # individual NPIs under a group TIN, so an org-only target set relies on
    # TIN matches there. See README "Type-1 vs Type-2 NPIs".
    include_individuals: bool = False


class HttpConfig(BaseModel):
    user_agent: str = "bcbs-mo-mrf-research/0.1"
    timeout_seconds: float = 120.0
    max_retries: int = 5
    backoff_base_seconds: float = 2.0


class PathsConfig(BaseModel):
    raw_dir: Path = Path("data/raw")
    out_dir: Path = Path("data/out")


class Config(BaseModel):
    payers: PayersConfig = Field(default_factory=PayersConfig)
    npi_targets: NpiTargetsConfig
    billing_codes: list[str]
    http: HttpConfig = Field(default_factory=HttpConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @field_validator("billing_codes")
    @classmethod
    def codes_as_str(cls, v: list) -> list[str]:
        return [str(c).strip() for c in v]

    @property
    def billing_code_set(self) -> frozenset[str]:
        return frozenset(self.billing_codes)


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
