import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.database import DbSession
from app.models import DataPointSeries, EventRecord, EventRecordDetail, HealthScore, User
from app.repositories import (
    DataPointSeriesRepository,
    DataSourceRepository,
    EventRecordDetailRepository,
    EventRecordRepository,
    HealthScoreRepository,
    UserConnectionRepository,
    UserRepository,
)
from app.repositories.provider_native_record_repository import NativeWriteStatus, ProviderNativeRecordRepository
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    HealthScoreCreate,
    ScoreComponent,
)
from app.schemas.providers.garmin.bridge_import import (
    GarminBridgeImportRequest,
    GarminBridgeImportResponse,
    GarminBridgeRecord,
    GarminNormalizationError,
)
from app.services.event_record_service import event_record_service
from app.services.fit_parser import FIT_PARSER_NAME, FIT_PARSER_VERSION, FitParseResult
from app.services.providers.garmin.bridge_manifest import GARMIN_BRIDGE_NORMALIZATIONS
from app.services.providers.garmin.normalizer import GarminNormalizer
from app.services.scores.resilience_service import resilience_score_service

GARMIN_NATIVE_RECORD_MAX_BYTES = 256 * 1024

_SENSITIVE_KEY_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "mfa",
    "oauth",
    "otp",
    "passcode",
    "password",
    "securityanswer",
    "securityquestion",
    "secret",
    "sessiontoken",
    "token",
)
_SENSITIVE_EXACT_KEYS = {
    "accesskey",
    "accountsecurity",
    "apikey",
    "diclientid",
    "sessionid",
}
_SAMPLE_NORMALIZATIONS = {
    "dailies",
    "hrv",
    "pulse_ox",
    "respiration",
    "body_compositions",
    "stress_details",
    "blood_pressures",
    "skin_temperature",
    "user_metrics",
}
FitDecodeStatus = Literal["decoded", "invalid_crc", "decode_failed", "decode_partial"]


class GarminImportValidationError(ValueError):
    pass


@dataclass
class GarminFitWrite:
    status: NativeWriteStatus
    object_key: str
    delete_object_key: str | None
    decode_status: FitDecodeStatus
    decoded_message_count: int


def _normalized_key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def scrub_garmin_payload(value: Any) -> Any:
    """Recursively remove account-security material using deterministic key rules."""
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key in sorted(value):
            normalized = _normalized_key(str(key))
            if normalized in _SENSITIVE_EXACT_KEYS or any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
                continue
            scrubbed[str(key)] = scrub_garmin_payload(value[key])
        return scrubbed
    if isinstance(value, list):
        return [scrub_garmin_payload(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class GarminBridgeImportService:
    def __init__(self) -> None:
        self.normalizer = GarminNormalizer()
        self.user_repo = UserRepository(User)
        self.native_repo = ProviderNativeRecordRepository()
        self.event_repo = EventRecordRepository(EventRecord)
        self.detail_repo = EventRecordDetailRepository(EventRecordDetail)
        self.sample_repo = DataPointSeriesRepository(DataPointSeries)
        self.score_repo = HealthScoreRepository(HealthScore)
        self.data_source_repo = DataSourceRepository()
        self.connection_repo = UserConnectionRepository()

    def validate_endpoint(self, kind: str, source_method: str) -> str | None:
        key = (kind, source_method)
        if key not in GARMIN_BRIDGE_NORMALIZATIONS:
            raise GarminImportValidationError("unsupported_kind_source_method")
        return GARMIN_BRIDGE_NORMALIZATIONS[key]

    def validate_user(self, db: DbSession, user_id: UUID) -> None:
        if self.user_repo.get(db, user_id) is None:
            raise GarminImportValidationError("user_not_found")

    def import_batch(
        self,
        db: DbSession,
        user_id: UUID,
        request: GarminBridgeImportRequest,
    ) -> GarminBridgeImportResponse:
        self.validate_user(db, user_id)
        normalization = self.validate_endpoint(request.kind, request.source_method)
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        errors: list[GarminNormalizationError] = []
        observed_at = datetime.now(timezone.utc)

        for record in request.records:
            payload = scrub_garmin_payload({"canonical": record.canonical, "native": record.native})
            payload_bytes = canonical_json_bytes(payload)
            if len(payload_bytes) > GARMIN_NATIVE_RECORD_MAX_BYTES:
                raise GarminImportValidationError("native_record_too_large")
            payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
            existing_native = self.native_repo.get_by_identity(
                db,
                user_id,
                request.kind,
                record.provider_record_id,
            )
            previous_recorded_at = existing_native.source_recorded_at if existing_native is not None else None
            write_status = self.native_repo.upsert(
                db,
                user_id=user_id,
                kind=request.kind,
                provider_record_id=record.provider_record_id,
                source_method=request.source_method,
                source_recorded_at=record.source_recorded_at,
                source_updated_at=record.source_updated_at,
                observed_at=observed_at,
                contract_version=request.contract_version,
                payload_sha256=payload_sha256,
                payload=payload,
            )
            counts[write_status] += 1

            if normalization is None:
                continue
            native_record = existing_native or self.native_repo.get_by_identity(
                db,
                user_id,
                request.kind,
                record.provider_record_id,
            )
            if native_record is None:
                raise RuntimeError("native Garmin record was not persisted")
            if write_status == "unchanged" and native_record.normalized_payload_sha256 == payload_sha256:
                continue

            savepoint = db.begin_nested()
            try:
                canonical = payload["canonical"]
                if not isinstance(canonical, dict):
                    raise GarminImportValidationError("canonical_record_not_normalizable")
                self._normalize_record(
                    db,
                    user_id,
                    normalization,
                    record,
                    canonical,
                    previous_recorded_at=previous_recorded_at,
                )
                self.native_repo.mark_normalized(db, native_record, payload_sha256)
                savepoint.commit()
            except SQLAlchemyError:
                savepoint.rollback()
                raise
            except Exception as exc:
                savepoint.rollback()
                # Some legacy repositories translate integrity errors into an
                # HTTPException. Never report an infrastructure failure as
                # harmless upstream schema drift (or expose its DB detail).
                if isinstance(exc.__cause__, SQLAlchemyError):
                    raise exc.__cause__
                errors.append(
                    GarminNormalizationError(
                        provider_record_id=record.provider_record_id,
                        code="schema_drift",
                    )
                )

        return GarminBridgeImportResponse(
            batch_id=request.batch_id,
            committed=sum(counts.values()),
            inserted=counts["inserted"],
            updated=counts["updated"],
            unchanged=counts["unchanged"],
            rejected=0,
            normalization_errors=len(errors),
            errors=errors,
        )

    def _normalize_record(
        self,
        db: DbSession,
        user_id: UUID,
        normalization: str,
        record: GarminBridgeRecord,
        canonical: dict[str, Any],
        *,
        previous_recorded_at: datetime | None,
    ) -> None:
        samples = []
        scores: list[HealthScoreCreate] = []
        event_bundle: tuple[EventRecordCreate, EventRecordDetailCreate | None, str | None] | None = None

        match normalization:
            case "sleeps":
                normalized, score = self.normalizer.normalize_sleep(canonical, user_id)
                result = self.normalizer._build_sleep_record(user_id, normalized)
                if result is not None:
                    event_bundle = (*result, "sleep")
                if score is not None:
                    scores.append(score)
            case "dailies":
                normalized, scores = self.normalizer.normalize_dailies(canonical, user_id)
                samples = self.normalizer._build_dailies_samples(user_id, normalized)
            case "hrv":
                samples = self.normalizer._build_hrv_samples(user_id, canonical)
            case "pulse_ox":
                samples = self.normalizer._build_pulse_ox_samples(user_id, canonical)
            case "respiration":
                samples = self.normalizer._build_respiration_samples(user_id, canonical)
            case "body_compositions":
                samples = self.normalizer._build_body_comp_samples(user_id, canonical)
            case "stress_details":
                samples = self.normalizer._build_stress_samples(user_id, canonical)
                score = self.normalizer._normalize_body_battery_health_score(user_id, canonical)
                if score is not None:
                    scores.append(score)
            case "blood_pressures":
                samples = self.normalizer._build_blood_pressure_samples(user_id, canonical)
            case "skin_temperature":
                samples = self.normalizer._build_skin_temp_samples(user_id, canonical)
            case "user_metrics":
                samples = self.normalizer._build_user_metrics_samples(user_id, canonical)
            case "activities":
                result = self.normalizer._build_activity_record(user_id, canonical)
                if result is not None:
                    event_bundle = (*result, "workout")
            case "activity_details":
                result = self.normalizer._build_activity_record(user_id, canonical.get("summary", canonical))
                if result is not None:
                    event_bundle = (*result, "workout")
            case "training_readiness":
                if str(canonical.get("inputContext", "")).upper() != "POST_WAKEUP":
                    return
                score_value = canonical.get("score")
                if score_value is None:
                    score_value = canonical.get("trainingReadinessScore", canonical.get("readinessScore"))
                if score_value is not None:
                    scores.append(
                        HealthScoreCreate(
                            id=uuid4(),
                            user_id=user_id,
                            provider=ProviderName.GARMIN,
                            category=HealthScoreCategory.READINESS,
                            value=Decimal(str(score_value)),
                            qualifier=canonical.get("level"),
                            recorded_at=record.source_recorded_at,
                        )
                    )
            case _:
                raise GarminImportValidationError("unsupported_normalization")

        if not samples and not scores and event_bundle is None:
            raise GarminImportValidationError("canonical_record_not_normalizable")

        event_id: UUID | None = None
        if event_bundle is not None:
            event, detail, detail_type = event_bundle
            event.external_id = record.provider_record_id
            event.provider = ProviderName.GARMIN.value
            saved_event, _ = self.event_repo.upsert_by_external_id(db, event)
            event_id = saved_event.id
            if detail is not None and detail_type is not None:
                saved_detail = detail.model_copy(update={"record_id": event_id})
                if detail_type == "workout" and self.detail_repo.get_by_record_id(db, event_id) is not None:
                    fields = saved_detail.model_dump(
                        exclude={"record_id", "segments", "hr_zones", "power_zones"},
                        exclude_unset=True,
                    )
                    if fields:
                        self.detail_repo.update_workout_fields(db, event_id, fields)
                else:
                    self.detail_repo.bulk_create(db, [saved_detail], detail_type=detail_type)

        if normalization in _SAMPLE_NORMALIZATIONS:
            projection_prefix = self._sample_projection_prefix(normalization, record.provider_record_id)
            self.sample_repo.delete_by_user_provider_external_prefix(
                db,
                user_id,
                ProviderName.GARMIN,
                projection_prefix,
            )
            samples = [
                sample.model_copy(update={"external_id": f"{projection_prefix}:{index}"})
                for index, sample in enumerate(samples)
            ]
        if samples:
            self.sample_repo.bulk_create(db, samples)
        for score in scores:
            if score.category == HealthScoreCategory.SLEEP and event_id is not None:
                score = score.model_copy(update={"sleep_record_id": event_id})
            self.score_repo.upsert(
                db,
                score,
                previous_recorded_at=(
                    previous_recorded_at if score.category == HealthScoreCategory.READINESS else None
                ),
            )

    @staticmethod
    def _sample_projection_prefix(normalization: str, provider_record_id: str) -> str:
        identity = f"{normalization}\0{provider_record_id}".encode()
        return f"garmin-bridge:{hashlib.sha256(identity).hexdigest()[:32]}"

    def write_fit_manifest(
        self,
        db: DbSession,
        *,
        user_id: UUID,
        activity_id: str,
        source_updated_at: datetime | None,
        fetched_at: datetime,
        object_key: str,
        payload_sha256: str,
        byte_count: int,
        fit_result: FitParseResult | None,
        decode_status: FitDecodeStatus,
        decode_error_category: str | None,
    ) -> tuple[GarminFitWrite, UUID]:
        self.validate_user(db, user_id)
        event = self.event_repo.get_by_external_id(
            db,
            user_id,
            activity_id,
            provider=ProviderName.GARMIN.value,
        )
        if event is None:
            raise GarminImportValidationError("activity_not_found")

        existing = self.native_repo.get_by_identity(db, user_id, "activity_fit_asset", activity_id)
        previous_key = existing.payload.get("object_key") if existing and isinstance(existing.payload, dict) else None
        manifest = {
            "activity_id": activity_id,
            "event_record_id": str(event.id),
            "object_key": object_key,
            "sha256": payload_sha256,
            "byte_count": byte_count,
            "fetched_at": fetched_at.isoformat(),
            "activity_source_updated_at": source_updated_at.isoformat() if source_updated_at else None,
            "ingest_contract_version": 1,
            "fit_protocol_version": fit_result.protocol_version if fit_result else None,
            "fit_profile_version": fit_result.profile_version if fit_result else None,
            "parser_name": FIT_PARSER_NAME,
            "parser_version": FIT_PARSER_VERSION,
            "decode_status": decode_status,
            "decode_error_category": decode_error_category,
            "decoded_message_count": fit_result.message_count if fit_result else 0,
            "message_counts": fit_result.message_counts if fit_result else {},
            "standard_field_names": fit_result.standard_fields_found if fit_result else [],
            "developer_field_names": fit_result.developer_fields_found if fit_result else [],
            "compact_metadata": fit_result.compact_metadata if fit_result else {},
            "compact_metadata_omitted": fit_result.compact_metadata_omitted if fit_result else {},
            "segment_records_omitted": fit_result.segment_records_omitted if fit_result else {},
            "field_names_omitted": fit_result.field_names_omitted if fit_result else {},
        }
        status = self.native_repo.upsert(
            db,
            user_id=user_id,
            kind="activity_fit_asset",
            provider_record_id=activity_id,
            source_method="download_original_activity",
            source_recorded_at=event.start_datetime,
            source_updated_at=source_updated_at,
            observed_at=fetched_at,
            contract_version=1,
            payload_sha256=payload_sha256,
            payload=manifest,
        )

        previous_payload = existing.payload if existing is not None and isinstance(existing.payload, dict) else None
        previous_decode_status = previous_payload.get("decode_status") if previous_payload else None
        previous_decoded_messages = previous_payload.get("decoded_message_count", 0) if previous_payload else 0
        previous_parser_version = previous_payload.get("parser_version") if previous_payload else None
        current_decode_succeeded = decode_status in ("decoded", "decode_partial")
        previous_decode_succeeded = previous_decode_status in ("decoded", "decode_partial")
        refresh_unchanged = (
            status == "unchanged"
            and current_decode_succeeded
            and (not previous_decode_succeeded or previous_parser_version != FIT_PARSER_VERSION)
        )

        if refresh_unchanged and existing is not None:
            self.native_repo.refresh_payload(
                db,
                existing,
                source_updated_at=source_updated_at,
                observed_at=fetched_at,
                payload=manifest,
            )

        if status != "unchanged" or refresh_unchanged:
            fields: dict[str, Any] = {
                "segments": [],
                "hr_zones": None,
                "power_zones": None,
            }
            if fit_result is not None and decode_status in ("decoded", "decode_partial"):
                fields.update(
                    segments=[*fit_result.segments, *fit_result.compact_segments],
                    hr_zones=fit_result.hr_zones.model_dump() if fit_result.hr_zones is not None else None,
                    power_zones=fit_result.power_zones.model_dump() if fit_result.power_zones is not None else None,
                )
            self.detail_repo.update_workout_fields(db, event.id, fields)

        if status == "unchanged" and not refresh_unchanged and previous_payload is not None:
            effective_object_key = previous_key or object_key
            effective_decode_status = cast(FitDecodeStatus, previous_decode_status or decode_status)
            effective_decoded_messages = int(previous_decoded_messages)
            delete_object_key = object_key if object_key != effective_object_key else None
        else:
            effective_object_key = object_key
            effective_decode_status = decode_status
            effective_decoded_messages = fit_result.message_count if fit_result else 0
            delete_object_key = previous_key if previous_key and previous_key != object_key else None

        return (
            GarminFitWrite(
                status=status,
                object_key=effective_object_key,
                delete_object_key=delete_object_key,
                decode_status=effective_decode_status,
                decoded_message_count=effective_decoded_messages,
            ),
            event.id,
        )

    def get_fit_event_id(self, db: DbSession, user_id: UUID, activity_id: str) -> UUID:
        self.validate_user(db, user_id)
        event = self.event_repo.get_by_external_id(
            db,
            user_id,
            activity_id,
            provider=ProviderName.GARMIN.value,
        )
        if event is None or event.category != "workout":
            raise GarminImportValidationError("activity_not_found")
        return event.id

    def purge_user(self, db: DbSession, user_id: UUID) -> list[str]:
        if self.user_repo.get(db, user_id) is None:
            return []
        object_keys = self.native_repo.get_fit_object_keys(db, user_id)
        sleep_dates: set[date] = {
            event_record_service._local_sleep_date(start_datetime, zone_offset)
            for start_datetime, zone_offset in self.event_repo.get_sleep_dates_by_user_provider(
                db,
                user_id,
                ProviderName.GARMIN.value,
            )
        }
        self.native_repo.delete_for_user(db, user_id)
        self.score_repo.delete_by_user_provider(db, user_id, ProviderName.GARMIN.value)
        sources_deleted = self.data_source_repo.delete_by_user_provider(db, user_id, ProviderName.GARMIN)
        self.connection_repo.delete_by_user_provider(db, user_id, ProviderName.GARMIN.value)
        db.flush()
        if sleep_dates:
            event_record_service._recompute_sleep_scores(db, user_id, sleep_dates)
        if sources_deleted:
            resilience_dates = {
                score.recorded_at.date()
                for score in db.query(HealthScore)
                .filter(
                    HealthScore.user_id == user_id,
                    HealthScore.provider == ProviderName.INTERNAL,
                    HealthScore.category == HealthScoreCategory.RESILIENCE,
                )
                .all()
            }
            self._recompute_resilience_scores(db, user_id, resilience_dates)
        return object_keys

    def _recompute_resilience_scores(self, db: DbSession, user_id: UUID, score_dates: set[date]) -> None:
        for score_date in score_dates:
            self.score_repo.delete_for_user_date(
                db,
                user_id,
                score_date,
                HealthScoreCategory.RESILIENCE,
            )
        scores_by_date = resilience_score_service.get_hrv_cv_scores_for_date_range(db, user_id, list(score_dates))
        creators = [
            HealthScoreCreate(
                id=uuid4(),
                user_id=user_id,
                provider=ProviderName.INTERNAL,
                category=HealthScoreCategory.RESILIENCE,
                value=result.hrv_cv,
                recorded_at=datetime(score_date.year, score_date.month, score_date.day, tzinfo=timezone.utc),
                components={
                    "days_counted": ScoreComponent(value=result.days_counted),
                    "metric_type": ScoreComponent(qualifier=result.metric_type),
                    "resilience_score": ScoreComponent(value=result.resilience_score),
                },
            )
            for score_date, result in scores_by_date.items()
            if result.hrv_cv is not None
        ]
        self.score_repo.bulk_create(db, creators)


garmin_bridge_import_service = GarminBridgeImportService()
