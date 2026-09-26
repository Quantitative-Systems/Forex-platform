"""Dataset provenance and reproducibility controls for research qualification.

Synthetic data remains useful for unit tests and smoke tests, but it must never be
indistinguishable from real broker/vendor data in a promotion artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Optional

import polars as pl


class DataProvenance(str, Enum):
    """Trust classification for a market-data dataset."""

    REAL_VENDOR = "REAL_VENDOR"
    BROKER_EXPORT = "BROKER_EXPORT"
    SYNTHETIC = "SYNTHETIC"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DatasetFingerprint:
    """Small, deterministic identity for a canonical OHLCV dataset."""

    rows: int
    first_timestamp: str | None
    last_timestamp: str | None
    content_hash: str

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "rows": self.rows,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "content_hash": self.content_hash,
        }


def fingerprint_dataframe(df: pl.DataFrame) -> DatasetFingerprint:
    """Hash the canonical market-data content without relying on file metadata."""
    if df.is_empty():
        return DatasetFingerprint(0, None, None, hashlib.sha256(b"empty").hexdigest())

    ordered = df.sort("timestamp") if "timestamp" in df.columns else df
    hasher = hashlib.sha256()
    for column in ordered.columns:
        hasher.update(column.encode("utf-8"))
        hasher.update(b"\0")
        series = ordered[column]
        for value in series.to_list():
            hasher.update(str(value).encode("utf-8"))
            hasher.update(b"\0")

    timestamp_values = ordered["timestamp"].to_list() if "timestamp" in ordered.columns else []
    return DatasetFingerprint(
        rows=ordered.height,
        first_timestamp=str(timestamp_values[0]) if timestamp_values else None,
        last_timestamp=str(timestamp_values[-1]) if timestamp_values else None,
        content_hash=hasher.hexdigest(),
    )


def is_qualifying_real_data(provenance: DataProvenance | str) -> bool:
    """Only real vendor or broker-exported data may qualify a candidate."""
    value = provenance.value if isinstance(provenance, DataProvenance) else str(provenance)
    return value in {DataProvenance.REAL_VENDOR.value, DataProvenance.BROKER_EXPORT.value}


def require_real_data(provenance: DataProvenance | str, context: str = "research qualification") -> None:
    """Raise a clear error instead of allowing unknown/synthetic data through."""
    if not is_qualifying_real_data(provenance):
        value = provenance.value if isinstance(provenance, DataProvenance) else str(provenance)
        raise ValueError(
            f"{context} requires real vendor or broker-exported data; received {value}. "
            "Synthetic data is restricted to tests and smoke tests."
        )


def classify_source(source: str) -> DataProvenance:
    """Conservatively classify a configured source name."""
    normalized = source.strip().lower()
    if any(token in normalized for token in ("synthetic", "random", "mock", "demo-generated")):
        return DataProvenance.SYNTHETIC
    if any(token in normalized for token in ("dukascopy", "histdata", "vendor", "broker", "mt5")):
        return DataProvenance.REAL_VENDOR
    return DataProvenance.UNKNOWN


def provenance_payload(
    provenance: DataProvenance | str,
    source: str,
    df: pl.DataFrame,
    *,
    notes: str = "",
) -> dict[str, object]:
    """Create an auditable provenance payload for research artifacts."""
    value = provenance.value if isinstance(provenance, DataProvenance) else str(provenance)
    return {
        "classification": value,
        "source": source,
        "qualifies_for_promotion": is_qualifying_real_data(value),
        "fingerprint": fingerprint_dataframe(df).as_dict(),
        "notes": notes,
    }
