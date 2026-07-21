import hashlib
import hmac
import json
import logging
import math
import re
from datetime import datetime, timezone
from functools import partial
from time import perf_counter
from typing import Annotated, Any
from uuid import UUID

from anyio import CapacityLimiter, to_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fitdecode import FitCRCError
from pydantic import ValidationError

from app.config import settings
from app.database import DbSession
from app.schemas.providers.garmin import (
    GarminBridgeImportRequest,
    GarminBridgeImportResponse,
    GarminFitImportResponse,
    GarminPurgeResponse,
)
from app.services.fit_parser import FIT_MAX_MESSAGES, FitMessageLimitError, FitParseResult, parse_fit_file
from app.services.providers.garmin.bridge_import import (
    FitDecodeStatus,
    GarminImportValidationError,
    garmin_bridge_import_service,
)
from app.services.raw_payload_storage import (
    FitStorageError,
    delete_fit_file,
    purge_fit_prefix,
    put_fit_file,
)
from app.utils.structured_logging import log_structured

router = APIRouter()
logger = logging.getLogger(__name__)

FIT_CONTENT_TYPE = "application/vnd.ant.fit"
NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _http_error(
    status_code: int,
    detail: str,
    *,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={**NO_STORE_HEADERS, **(headers or {})},
    )


def _authenticate_bridge(authorization: Annotated[str | None, Header()] = None) -> None:
    configured = settings.garmin_bridge_ingest_secret
    if configured is None or not configured.get_secret_value():
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "Garmin bridge ingest is disabled")
    scheme, separator, supplied = (authorization or "").partition(" ")
    expected = configured.get_secret_value()
    valid_format = separator == " " and scheme.lower() == "bearer"
    valid_secret = hmac.compare_digest(supplied, expected)
    if not valid_format or not valid_secret:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid bridge credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _read_limited_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise _http_error(status.HTTP_400_BAD_REQUEST, "Invalid Content-Length")
            if parsed_content_length > limit:
                raise _http_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        except ValueError as exc:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "Invalid Content-Length") from exc

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _http_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Request body too large")
    return bytes(body)


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite JSON number is not allowed: {value}")
    return parsed


def _fit_import_limiter(request: Request) -> CapacityLimiter:
    limiter = getattr(request.app.state, "garmin_fit_import_limiter", None)
    if limiter is None:
        limiter = CapacityLimiter(1)
        request.app.state.garmin_fit_import_limiter = limiter
    return limiter


def _parse_datetime_header(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid {name}") from exc
    if parsed.tzinfo is None:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid {name}")
    return parsed


def _handle_service_validation(exc: GarminImportValidationError) -> None:
    if str(exc) == "user_not_found":
        raise _http_error(status.HTTP_404_NOT_FOUND, "User not found") from exc
    if str(exc) == "activity_not_found":
        raise _http_error(status.HTTP_404_NOT_FOUND, "Garmin activity not found") from exc
    if str(exc) == "native_record_too_large":
        raise _http_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Garmin record too large") from exc
    raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported Garmin ingest contract") from exc


@router.post(
    "/internal/users/{user_id}/imports/garmin",
)
async def import_garmin_json(
    user_id: UUID,
    request: Request,
    response: Response,
    db: DbSession,
    _bridge_auth: Annotated[None, Depends(_authenticate_bridge)],
) -> GarminBridgeImportResponse:
    response.headers["Cache-Control"] = "no-store"
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise _http_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")

    try:
        garmin_bridge_import_service.validate_user(db, user_id)
    except GarminImportValidationError as exc:
        _handle_service_validation(exc)

    body = await _read_limited_body(request, settings.garmin_bridge_json_body_max_bytes)
    try:
        decoded: Any = json.loads(
            body,
            parse_constant=_reject_non_finite,
            parse_float=_parse_finite_float,
        )
        payload = GarminBridgeImportRequest.model_validate(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError, RecursionError) as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Garmin ingest payload") from exc

    try:
        result = garmin_bridge_import_service.import_batch(db, user_id, payload)
        db.commit()
    except GarminImportValidationError as exc:
        _handle_service_validation(exc)

    log_structured(
        logger,
        "info",
        "Committed Garmin bridge import",
        user_id=str(user_id),
        batch_id=str(payload.batch_id),
        kind=payload.kind,
        body_bytes=len(body),
        records=len(payload.records),
        inserted=result.inserted,
        updated=result.updated,
        unchanged=result.unchanged,
        normalization_errors=result.normalization_errors,
    )
    return result


@router.put(
    "/internal/users/{user_id}/imports/garmin/activities/{activity_id}/fit",
)
async def import_garmin_fit(
    user_id: UUID,
    activity_id: str,
    request: Request,
    response: Response,
    db: DbSession,
    _bridge_auth: Annotated[None, Depends(_authenticate_bridge)],
    x_garmin_contract_version: Annotated[int | None, Header()] = None,
    x_garmin_source_updated_at: Annotated[str | None, Header()] = None,
) -> GarminFitImportResponse:
    response.headers["Cache-Control"] = "no-store"
    if x_garmin_contract_version != 1:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported Garmin contract version")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", activity_id) is None:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Garmin activity ID")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != FIT_CONTENT_TYPE:
        raise _http_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"Content-Type must be {FIT_CONTENT_TYPE}")

    try:
        garmin_bridge_import_service.get_fit_event_id(db, user_id, activity_id)
    except GarminImportValidationError as exc:
        _handle_service_validation(exc)

    async with _fit_import_limiter(request):
        fit_bytes = await _read_limited_body(request, settings.garmin_bridge_fit_body_max_bytes)
        if len(fit_bytes) < 12 or fit_bytes[8:12] != b".FIT":
            raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid FIT header")

        source_updated_at = _parse_datetime_header(x_garmin_source_updated_at, "X-Garmin-Source-Updated-At")
        fetched_at = datetime.now(timezone.utc)
        payload_sha256 = hashlib.sha256(fit_bytes).hexdigest()
        request_started = perf_counter()
        storage_started = perf_counter()
        try:
            object_key = await to_thread.run_sync(
                partial(
                    put_fit_file,
                    provider="garmin",
                    fit_bytes=fit_bytes,
                    user_id=str(user_id),
                    activity_id=activity_id,
                )
            )
        except FitStorageError as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "FIT storage unavailable") from exc
        storage_ms = round((perf_counter() - storage_started) * 1000, 1)

        fit_result: FitParseResult | None = None
        decode_status: FitDecodeStatus = "decoded"
        decode_error_category: str | None = None
        parse_started = perf_counter()
        try:
            fit_result = await to_thread.run_sync(
                partial(
                    parse_fit_file,
                    fit_bytes,
                    user_id,
                    source="garmin",
                    collect_samples=False,
                    max_messages=FIT_MAX_MESSAGES,
                )
            )
        except FitMessageLimitError:
            decode_status = "decode_failed"
            decode_error_category = "message_limit"
        except FitCRCError:
            decode_status = "invalid_crc"
            decode_error_category = "invalid_crc"
        except Exception:
            decode_status = "decode_failed"
            decode_error_category = "parser_error"
        parse_ms = round((perf_counter() - parse_started) * 1000, 1)

        try:
            fit_write, event_id = garmin_bridge_import_service.write_fit_manifest(
                db,
                user_id=user_id,
                activity_id=activity_id,
                source_updated_at=source_updated_at,
                fetched_at=fetched_at,
                object_key=object_key,
                payload_sha256=payload_sha256,
                byte_count=len(fit_bytes),
                fit_result=fit_result,
                decode_status=decode_status,
                decode_error_category=decode_error_category,
            )
            db.commit()
        except GarminImportValidationError as exc:
            _handle_service_validation(exc)

    if fit_write.delete_object_key:
        delete_fit_file(fit_write.delete_object_key, best_effort=True)

    log_structured(
        logger,
        "info",
        "Committed Garmin FIT asset",
        user_id=str(user_id),
        activity_id=activity_id,
        byte_count=len(fit_bytes),
        decoded_messages=fit_write.decoded_message_count,
        decode_status=fit_write.decode_status,
        deduplicated=fit_write.status == "unchanged",
        storage_ms=storage_ms,
        parse_ms=parse_ms,
        duration_ms=round((perf_counter() - request_started) * 1000, 1),
    )
    return GarminFitImportResponse(
        activity_id=activity_id,
        event_record_id=event_id,
        status=fit_write.status,
        object_key=fit_write.object_key,
        payload_sha256=payload_sha256,
        byte_count=len(fit_bytes),
        decode_status=fit_write.decode_status,
        decoded_messages=fit_write.decoded_message_count,
    )


@router.delete("/internal/users/{user_id}/imports/garmin")
def purge_garmin_data(
    user_id: UUID,
    response: Response,
    db: DbSession,
    _bridge_auth: Annotated[None, Depends(_authenticate_bridge)],
) -> GarminPurgeResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        result, object_keys = garmin_bridge_import_service.purge_user(db, user_id)
        deleted_objects = purge_fit_prefix("garmin", str(user_id), required=bool(object_keys))
        result.fit_objects_deleted = deleted_objects
        db.commit()
    except GarminImportValidationError as exc:
        _handle_service_validation(exc)
    except FitStorageError as exc:
        db.rollback()
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "FIT storage unavailable") from exc
    return result
