from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GARMIN_IMPORT_CONTRACT_VERSION = 1
GARMIN_IMPORT_MAX_RECORDS = 50


class GarminBridgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_record_id: str = Field(min_length=1, max_length=255)
    source_recorded_at: datetime
    source_updated_at: datetime | None = None
    payload_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    canonical: dict[str, Any]
    native: dict[str, Any]

    @field_validator("provider_record_id")
    @classmethod
    def validate_provider_record_id(cls, value: str) -> str:
        if value != value.strip() or any(not char.isprintable() for char in value):
            raise ValueError("provider_record_id must be trimmed printable text")
        return value

    @field_validator("source_recorded_at", "source_updated_at")
    @classmethod
    def validate_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("source timestamps must include a UTC offset")
        return value


class GarminBridgeImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1]
    batch_id: UUID
    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    source_method: str = Field(min_length=1, max_length=100, pattern=r"^get_[a-z0-9_]+$")
    records: list[GarminBridgeRecord] = Field(min_length=1, max_length=GARMIN_IMPORT_MAX_RECORDS)

    @model_validator(mode="after")
    def validate_unique_record_ids(self) -> "GarminBridgeImportRequest":
        record_ids = [record.provider_record_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("provider_record_id values must be unique within a batch")
        if self.kind in {"activities", "activity_details", "sleeps"} and any(
            len(record_id) > 100 for record_id in record_ids
        ):
            raise ValueError("event provider_record_id values must be at most 100 characters")
        return self


class GarminNormalizationError(BaseModel):
    provider_record_id: str
    code: str


class GarminBridgeImportResponse(BaseModel):
    batch_id: UUID
    committed: int
    inserted: int
    updated: int
    unchanged: int
    rejected: int
    normalization_errors: int
    errors: list[GarminNormalizationError] = Field(default_factory=list)


class GarminFitImportResponse(BaseModel):
    activity_id: str
    event_record_id: UUID
    status: Literal["inserted", "updated", "unchanged"]
    object_key: str
    payload_sha256: str
    byte_count: int
    decode_status: Literal["decoded", "invalid_crc", "decode_failed", "decode_partial"]
    decoded_messages: int


class GarminPurgeResponse(BaseModel):
    user_id: UUID
    provider: Literal["garmin"] = "garmin"
    native_records_deleted: int
    data_sources_deleted: int
    health_scores_deleted: int
    connections_deleted: int
    fit_objects_deleted: int
