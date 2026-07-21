from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_64, str_100, str_255
from app.schemas.enums import ProviderName


class ProviderNativeRecord(BaseDbModel):
    """Scrubbed provider observation retained alongside normalized projections."""

    __tablename__ = "provider_native_record"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "kind",
            "provider_record_id",
            name="uq_provider_native_record_identity",
        ),
        Index(
            "ix_provider_native_record_user_kind_recorded",
            "user_id",
            "kind",
            "source_recorded_at",
        ),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    provider: Mapped[ProviderName]
    kind: Mapped[str_64]
    provider_record_id: Mapped[str_255]
    source_method: Mapped[str_100]
    source_recorded_at: Mapped[datetime]
    source_updated_at: Mapped[datetime | None]
    observed_at: Mapped[datetime]
    contract_version: Mapped[int]
    payload_sha256: Mapped[str_64]
    normalized_payload_sha256: Mapped[str_64 | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
