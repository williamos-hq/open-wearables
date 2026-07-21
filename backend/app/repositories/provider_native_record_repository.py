from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models import ProviderNativeRecord
from app.schemas.enums import ProviderName

NativeWriteStatus = Literal["inserted", "updated", "unchanged"]


class ProviderNativeRecordRepository:
    """Database operations for immutable-identity provider observations."""

    def upsert(
        self,
        db_session: DbSession,
        *,
        user_id: UUID,
        kind: str,
        provider_record_id: str,
        source_method: str,
        source_recorded_at: datetime,
        source_updated_at: datetime | None,
        observed_at: datetime,
        contract_version: int,
        payload_sha256: str,
        payload: dict[str, Any],
    ) -> NativeWriteStatus:
        stmt = insert(ProviderNativeRecord).values(
            id=uuid4(),
            user_id=user_id,
            provider=ProviderName.GARMIN,
            kind=kind,
            provider_record_id=provider_record_id,
            source_method=source_method,
            source_recorded_at=source_recorded_at,
            source_updated_at=source_updated_at,
            observed_at=observed_at,
            contract_version=contract_version,
            payload_sha256=payload_sha256,
            payload=payload,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_provider_native_record_identity",
            set_={
                "source_method": stmt.excluded.source_method,
                "source_recorded_at": stmt.excluded.source_recorded_at,
                "source_updated_at": stmt.excluded.source_updated_at,
                "observed_at": stmt.excluded.observed_at,
                "contract_version": stmt.excluded.contract_version,
                "payload_sha256": stmt.excluded.payload_sha256,
                "payload": stmt.excluded.payload,
            },
            where=ProviderNativeRecord.payload_sha256 != stmt.excluded.payload_sha256,
        ).returning(literal_column("(xmax = 0)"))

        result = db_session.execute(stmt).scalar_one_or_none()
        if result is None:
            return "unchanged"
        return "inserted" if result else "updated"

    def mark_normalized(
        self,
        db_session: DbSession,
        record: ProviderNativeRecord,
        payload_sha256: str,
    ) -> None:
        """Record which native payload version was projected successfully."""
        record.normalized_payload_sha256 = payload_sha256
        db_session.flush()

    def refresh_payload(
        self,
        db_session: DbSession,
        record: ProviderNativeRecord,
        *,
        source_updated_at: datetime | None,
        observed_at: datetime,
        payload: dict[str, Any],
    ) -> None:
        """Refresh metadata for an unchanged object after a successful re-decode."""
        record.source_updated_at = source_updated_at
        record.observed_at = observed_at
        record.payload = payload
        db_session.flush()

    def get_fit_object_keys(self, db_session: DbSession, user_id: UUID) -> list[str]:
        rows = (
            db_session.query(ProviderNativeRecord.payload)
            .filter(
                ProviderNativeRecord.user_id == user_id,
                ProviderNativeRecord.provider == ProviderName.GARMIN,
                ProviderNativeRecord.kind == "activity_fit_asset",
            )
            .all()
        )
        return [
            key
            for (payload,) in rows
            if isinstance(payload, dict) and isinstance((key := payload.get("object_key")), str)
        ]

    def get_by_identity(
        self,
        db_session: DbSession,
        user_id: UUID,
        kind: str,
        provider_record_id: str,
    ) -> ProviderNativeRecord | None:
        return (
            db_session.query(ProviderNativeRecord)
            .filter(
                ProviderNativeRecord.user_id == user_id,
                ProviderNativeRecord.provider == ProviderName.GARMIN,
                ProviderNativeRecord.kind == kind,
                ProviderNativeRecord.provider_record_id == provider_record_id,
            )
            .one_or_none()
        )

    def delete_for_user(self, db_session: DbSession, user_id: UUID) -> int:
        return (
            db_session.query(ProviderNativeRecord)
            .filter(
                ProviderNativeRecord.user_id == user_id,
                ProviderNativeRecord.provider == ProviderName.GARMIN,
            )
            .delete(synchronize_session=False)
        )
