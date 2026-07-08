"""Partitioned Parquet output with an explicit, enforced schema."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .toc import SourceFile

log = logging.getLogger(__name__)

# `payer` and `date` are NOT physical columns: they live in the hive partition
# path (payer=<payer>/date=<yyyy-mm>/) and materialize as columns whenever the
# dataset is read with hive partitioning (src/query.py does this). Duplicating
# them inside the file would collide with the partition columns on read.
RECORD_SCHEMA = pa.schema(
    [
        pa.field("source_file_url", pa.string(), nullable=False),
        pa.field("last_updated_on", pa.string()),
        pa.field("npi", pa.string(), nullable=False),
        pa.field("tin", pa.string()),
        pa.field("org_name", pa.string()),
        pa.field("billing_code", pa.string(), nullable=False),
        pa.field("billing_code_type", pa.string(), nullable=False),
        pa.field("billing_code_modifier", pa.string()),  # '|'-joined, '' = no modifier
        pa.field("negotiated_rate", pa.float64(), nullable=False),
        pa.field("negotiated_type", pa.string()),
        pa.field("billing_class", pa.string()),
        pa.field("service_code", pa.list_(pa.string())),
        pa.field("plan_name", pa.string()),
        pa.field("plan_id", pa.string()),
        pa.field("extracted_at", pa.string(), nullable=False),
    ]
)


class RecordWriter:
    """Writes one Parquet part per extracted source file under
    data/out/payer=<payer>/date=<yyyy-mm>/part-<urlhash>.parquet.

    Re-extracting the same source file overwrites its part, so re-runs stay
    idempotent per file.
    """

    def __init__(self, out_dir: Path, run_month: str | None = None):
        self.out_dir = Path(out_dir)
        self.run_month = run_month or dt.date.today().strftime("%Y-%m")

    def _part_path(self, source: SourceFile) -> Path:
        key = hashlib.sha1(source.url.split("?", 1)[0].encode()).hexdigest()[:16]
        d = self.out_dir / f"payer={source.payer}" / f"date={self.run_month}"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"part-{key}.parquet"

    def write(self, source: SourceFile, rows: list[dict]) -> Path | None:
        if not rows:
            return None
        table = pa.Table.from_pylist(rows, schema=RECORD_SCHEMA)
        path = self._part_path(source)
        pq.write_table(table, path)
        log.info("wrote %d rows -> %s", len(rows), path)
        return path
