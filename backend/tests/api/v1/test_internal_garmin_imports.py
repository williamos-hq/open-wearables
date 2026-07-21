import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    DataPointSeries,
    DataSource,
    EventRecord,
    HealthScore,
    ProviderNativeRecord,
    SleepDetails,
    User,
    WorkoutDetails,
)
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType, get_series_type_id
from app.services.providers.garmin.bridge_manifest import GARMIN_BRIDGE_ENDPOINTS
from app.services.raw_payload_storage import FitStorageError
from tests.fixtures.fit_builder import make_running_fit

BRIDGE_SECRET = "test-garmin-bridge-ingest-secret"


def test_checked_in_manifest_contains_only_bounded_named_pairs() -> None:
    assert GARMIN_BRIDGE_ENDPOINTS
    for (kind, source_method), endpoint in GARMIN_BRIDGE_ENDPOINTS.items():
        assert endpoint.kind == kind
        assert endpoint.source_method == source_method
        assert source_method.startswith("get_")
        assert endpoint.request_policy
        assert 1 <= endpoint.max_records <= 50


def _headers(content_type: str = "application/json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {BRIDGE_SECRET}",
        "Content-Type": content_type,
    }


def _batch(kind: str, source_method: str, provider_record_id: str, canonical: dict, native: dict) -> dict:
    return {
        "contract_version": 1,
        "batch_id": str(uuid4()),
        "kind": kind,
        "source_method": source_method,
        "records": [
            {
                "provider_record_id": provider_record_id,
                "source_recorded_at": "2026-07-19T12:00:00Z",
                "source_updated_at": "2026-07-19T13:00:00Z",
                "payload_sha256": "a" * 64,
                "canonical": canonical,
                "native": native,
            }
        ],
    }


def _daily(steps: int) -> dict:
    return {
        "summaryId": "daily-2026-07-19",
        "calendarDate": "2026-07-19",
        "startTimeInSeconds": 1_753_012_800,
        "startTimeOffsetInSeconds": -25_200,
        "steps": steps,
        "distanceInMeters": 6_200,
        "activeKilocalories": 420,
        "restingHeartRateInBeatsPerMinute": 51,
    }


def _activity(duration: int = 1800, start: int = 1_753_012_800, distance: int = 5_000) -> dict:
    return {
        "activityId": 987654,
        "activityType": "RUNNING",
        "startTimeInSeconds": start,
        "startTimeOffsetInSeconds": -25_200,
        "durationInSeconds": duration,
        "distanceInMeters": distance,
        "activeKilocalories": 350,
        "averageHeartRateInBeatsPerMinute": 148,
        "maxHeartRateInBeatsPerMinute": 172,
        "deviceName": "Forerunner",
    }


def _sleep(duration: int = 28_800, score: int = 82) -> dict:
    return {
        "summaryId": "sleep-2026-07-19",
        "calendarDate": "2026-07-19",
        "startTimeInSeconds": 1_753_012_800,
        "startTimeOffsetInSeconds": -25_200,
        "durationInSeconds": duration,
        "deepSleepDurationInSeconds": 7_200,
        "lightSleepDurationInSeconds": 14_400,
        "remSleepInSeconds": 5_400,
        "awakeDurationInSeconds": 1_800,
        "overallSleepScore": {"value": score, "qualifier": "GOOD"},
    }


def test_bridge_auth_is_dedicated_and_fail_closed(client: TestClient, user: User) -> None:
    path = f"/api/v1/internal/users/{user.id}/imports/garmin"
    payload = _batch("dailies", "get_stats", "daily-1", _daily(8000), {})
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert client.post(path, json=payload).status_code == 401
        assert client.post(path, json=payload, headers={"X-API-Key": str(uuid4())}).status_code == 401
        wrong_secret = {**_headers(), "Authorization": "Bearer wrong"}
        assert client.post(path, json=payload, headers=wrong_secret).status_code == 401


def test_json_import_scrubs_hashes_normalizes_and_replays_idempotently(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id = user.id
    path = f"/api/v1/internal/users/{user_id}/imports/garmin"
    native = {
        "unknownUsefulField": {"kept": True},
        "accessToken": "must-not-survive",
        "apiKey": "must-not-survive",
        "diClientId": "must-not-survive",
        "nested": {
            "cookies": "must-not-survive",
            "accountSecurityQuestion": "must-not-survive",
            "passwordHash": "must-not-survive",
            "sensor": "hrm-pro",
        },
    }
    canonical = {**_daily(8000), "authorization": "must-not-survive"}
    payload = _batch("dailies", "get_stats", "daily-1", canonical, native)

    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        first = client.post(path, json=payload, headers=_headers())
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        first_body = first.json()
        assert first_body == {
            "batch_id": payload["batch_id"],
            "committed": 1,
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "rejected": 0,
            "normalization_errors": 0,
            "errors": [],
        }

        row = db.query(ProviderNativeRecord).one()
        assert row.user_id == user_id
        assert row.provider == ProviderName.GARMIN
        assert row.payload["native"]["unknownUsefulField"] == {"kept": True}
        assert "authorization" not in row.payload["canonical"]
        assert "accessToken" not in row.payload["native"]
        assert "apiKey" not in row.payload["native"]
        assert "diClientId" not in row.payload["native"]
        assert "cookies" not in row.payload["native"]["nested"]
        assert "accountSecurityQuestion" not in row.payload["native"]["nested"]
        assert "passwordHash" not in row.payload["native"]["nested"]
        assert row.payload_sha256 != "a" * 64

        data_source = db.query(DataSource).filter_by(user_id=user_id, provider=ProviderName.GARMIN).one()
        steps = (
            db.query(DataPointSeries)
            .filter(
                DataPointSeries.data_source_id == data_source.id,
                DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.steps),
            )
            .one()
        )
        assert steps.value == Decimal(8000)

        replay = client.post(path, json={**payload, "batch_id": str(uuid4())}, headers=_headers())
        assert replay.status_code == 200
        assert replay.json()["unchanged"] == 1

        changed = deepcopy(payload)
        changed["batch_id"] = str(uuid4())
        changed["records"][0]["canonical"] = _daily(9000)
        changed_response = client.post(path, json=changed, headers=_headers())
        assert changed_response.status_code == 200
        assert changed_response.json()["updated"] == 1
        updated_steps = (
            db.query(DataPointSeries)
            .filter(
                DataPointSeries.data_source_id == data_source.id,
                DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.steps),
            )
            .one()
        )
        assert updated_steps.value == Decimal(9000)


def test_activity_correction_preserves_event_id_and_updates_details(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id = user.id
    path = f"/api/v1/internal/users/{user_id}/imports/garmin"
    payload = _batch("activities", "get_activities_by_date", "987654", _activity(), {"revision": 1})

    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert client.post(path, json=payload, headers=_headers()).status_code == 200
        event = db.query(EventRecord).filter_by(external_id="987654").one()
        original_id = event.id

        changed = _batch(
            "activities",
            "get_activities_by_date",
            "987654",
            _activity(duration=1900, start=1_753_012_860, distance=5_200),
            {"revision": 2},
        )
        response = client.post(path, json=changed, headers=_headers())
        assert response.status_code == 200
        assert response.json()["updated"] == 1

    db.expire_all()
    corrected = db.query(EventRecord).filter_by(external_id="987654").one()
    assert corrected.id == original_id
    assert corrected.duration_seconds == 1900
    assert corrected.start_datetime.timestamp() == 1_753_012_860
    details = db.query(WorkoutDetails).filter_by(record_id=original_id).one()
    assert details.distance == Decimal(5200)


def test_sleep_and_readiness_corrections_update_scores_in_place(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    path = f"/api/v1/internal/users/{user.id}/imports/garmin"
    sleep = _batch("sleeps", "get_sleep_data", "sleep-2026-07-19", _sleep(), {})
    readiness = _batch(
        "training_readiness",
        "get_training_readiness",
        "readiness-2026-07-19-post-wakeup",
        {"score": 76, "level": "MODERATE", "inputContext": "POST_WAKEUP"},
        {},
    )

    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert client.post(path, json=sleep, headers=_headers()).status_code == 200
        assert client.post(path, json=readiness, headers=_headers()).status_code == 200

        sleep_event = db.query(EventRecord).filter_by(external_id="sleep-2026-07-19").one()
        sleep_score = db.query(HealthScore).filter_by(category=HealthScoreCategory.SLEEP).one()
        readiness_score = db.query(HealthScore).filter_by(category=HealthScoreCategory.READINESS).one()
        sleep_event_id = sleep_event.id
        sleep_score_id = sleep_score.id
        readiness_score_id = readiness_score.id

        corrected_sleep = deepcopy(sleep)
        corrected_sleep["batch_id"] = str(uuid4())
        corrected_sleep["records"][0]["canonical"] = _sleep(duration=29_100, score=88)
        corrected_readiness = deepcopy(readiness)
        corrected_readiness["batch_id"] = str(uuid4())
        corrected_readiness["records"][0]["canonical"]["score"] = 81
        corrected_readiness["records"][0]["source_recorded_at"] = "2026-07-19T14:00:00Z"
        during_sleep = _batch(
            "training_readiness",
            "get_training_readiness",
            "readiness-2026-07-19-during-sleep",
            {"score": 55, "level": "LOW", "inputContext": "DURING_SLEEP"},
            {},
        )
        assert client.post(path, json=corrected_sleep, headers=_headers()).status_code == 200
        assert client.post(path, json=corrected_readiness, headers=_headers()).status_code == 200
        assert client.post(path, json=during_sleep, headers=_headers()).status_code == 200

    db.expire_all()
    corrected_event = db.query(EventRecord).filter_by(external_id="sleep-2026-07-19").one()
    corrected_detail = db.query(SleepDetails).filter_by(record_id=corrected_event.id).one()
    corrected_sleep_score = db.query(HealthScore).filter_by(category=HealthScoreCategory.SLEEP).one()
    corrected_readiness_score = db.query(HealthScore).filter_by(category=HealthScoreCategory.READINESS).one()
    assert corrected_event.id == sleep_event_id
    assert corrected_event.duration_seconds == 29_100
    assert corrected_detail.sleep_total_duration_minutes == 450
    assert corrected_detail.sleep_time_in_bed_minutes == 485
    assert corrected_sleep_score.id == sleep_score_id
    assert corrected_sleep_score.value == Decimal(88)
    assert corrected_readiness_score.id == readiness_score_id
    assert corrected_readiness_score.value == Decimal(81)
    assert corrected_readiness_score.qualifier == "MODERATE"
    assert corrected_readiness_score.recorded_at == datetime(2026, 7, 19, 14, tzinfo=timezone.utc)
    assert db.query(HealthScore).filter_by(category=HealthScoreCategory.READINESS).count() == 1
    assert db.query(ProviderNativeRecord).filter_by(kind="training_readiness").count() == 2


def test_normalization_drift_commits_native_record_with_explicit_error(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id = user.id
    payload = _batch("activities", "get_activities_by_date", "broken-1", {"unexpected": True}, {"raw": 1})
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        response = client.post(
            f"/api/v1/internal/users/{user_id}/imports/garmin",
            json=payload,
            headers=_headers(),
        )
        replay = client.post(
            f"/api/v1/internal/users/{user_id}/imports/garmin",
            json={**payload, "batch_id": str(uuid4())},
            headers=_headers(),
        )
    assert response.status_code == 200
    assert response.json()["committed"] == 1
    assert response.json()["normalization_errors"] == 1
    assert response.json()["errors"] == [{"provider_record_id": "broken-1", "code": "schema_drift"}]
    assert replay.status_code == 200
    assert replay.json()["unchanged"] == 1
    assert replay.json()["normalization_errors"] == 1
    native = db.query(ProviderNativeRecord).filter_by(provider_record_id="broken-1").one()
    assert native.normalized_payload_sha256 is None


def test_timeseries_correction_replaces_the_record_projection(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    path = f"/api/v1/internal/users/{user.id}/imports/garmin"
    original = _batch("dailies", "get_stats", "daily-1", _daily(8000), {})
    corrected_daily = _daily(9000)
    corrected_daily["startTimeInSeconds"] += 60
    corrected_daily.pop("distanceInMeters")
    corrected = _batch("dailies", "get_stats", "daily-1", corrected_daily, {"revision": 2})

    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert client.post(path, json=original, headers=_headers()).status_code == 200
        old_timestamps = {row.recorded_at for row in db.query(DataPointSeries).all()}
        assert client.post(path, json=corrected, headers=_headers()).status_code == 200

    rows = db.query(DataPointSeries).all()
    assert rows
    assert old_timestamps.isdisjoint({row.recorded_at for row in rows})
    distance_type_id = get_series_type_id(SeriesType.distance_walking_running)
    assert all(row.series_type_definition_id != distance_type_id for row in rows)


def test_contract_rejects_identity_fields_unknown_pairs_and_oversized_body(
    client: TestClient,
    user: User,
) -> None:
    path = f"/api/v1/internal/users/{user.id}/imports/garmin"
    payload = _batch("dailies", "get_stats", "daily-1", _daily(8000), {})
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        injected = {**payload, "provider": "garmin", "user_id": str(user.id)}
        assert client.post(path, json=injected, headers=_headers()).status_code == 422
        unknown = {**payload, "source_method": "get_arbitrary_endpoint"}
        assert client.post(path, json=unknown, headers=_headers()).status_code == 422
        naive = deepcopy(payload)
        naive["records"][0]["source_recorded_at"] = "2026-07-19T12:00:00"
        assert client.post(path, json=naive, headers=_headers()).status_code == 422
        too_long_event_id = deepcopy(payload)
        too_long_event_id["kind"] = "activities"
        too_long_event_id["source_method"] = "get_activities_by_date"
        too_long_event_id["records"][0]["provider_record_id"] = "a" * 101
        assert client.post(path, json=too_long_event_id, headers=_headers()).status_code == 422
        exponent_body = json.dumps(payload).replace('"steps": 8000', '"steps": 1e9999')
        assert client.post(path, content=exponent_body, headers=_headers()).status_code == 422
        with patch.object(settings, "garmin_bridge_json_body_max_bytes", 128):
            assert client.post(path, json=payload, headers=_headers()).status_code == 413


def test_fit_import_retains_source_without_expanding_samples(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id: UUID = user.id
    json_path = f"/api/v1/internal/users/{user_id}/imports/garmin"
    activity_payload = _batch("activities", "get_activities_by_date", "987654", _activity(), {})
    fit_bytes = make_running_fit()
    expected_key = f"fit-files/garmin/{user_id}/987654/{'b' * 64}.fit"

    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert client.post(json_path, json=activity_payload, headers=_headers()).status_code == 200
        with patch(
            "app.api.routes.v1.internal_garmin_imports.put_fit_file",
            return_value=expected_key,
        ):
            response = client.put(
                f"{json_path}/activities/987654/fit",
                content=fit_bytes,
                headers={
                    **_headers("application/vnd.ant.fit"),
                    "X-Garmin-Contract-Version": "1",
                    "X-Garmin-Source-Updated-At": "2026-07-19T13:00:00Z",
                },
            )
            replay = client.put(
                f"{json_path}/activities/987654/fit",
                content=fit_bytes,
                headers={**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"},
            )

        changed_key = f"fit-files/garmin/{user_id}/987654/{'c' * 64}.fit"
        with patch(
            "app.api.routes.v1.internal_garmin_imports.put_fit_file",
            return_value=changed_key,
        ):
            changed = client.put(
                f"{json_path}/activities/987654/fit",
                content=make_running_fit(n_records=21),
                headers={**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"},
            )

    assert response.status_code == 200
    assert response.json()["decode_status"] == "decoded"
    assert response.json()["decoded_messages"] > 0
    assert replay.status_code == 200
    assert replay.json()["status"] == "unchanged"
    assert changed.status_code == 200
    assert changed.json()["status"] == "updated"
    assert changed.json()["event_record_id"] == response.json()["event_record_id"]
    manifest = db.query(ProviderNativeRecord).filter_by(kind="activity_fit_asset").one()
    assert manifest.payload["object_key"] == changed_key
    assert manifest.payload["decode_status"] == changed.json()["decode_status"]
    assert db.query(DataPointSeries).count() == 0
    event = db.query(EventRecord).filter_by(external_id="987654").one()
    details = db.query(WorkoutDetails).filter_by(record_id=event.id).one()
    assert details.segments
    fit_segments = deepcopy(details.segments)

    correction = _batch("activities", "get_activities_by_date", "987654", _activity(distance=5_400), {})
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert client.post(json_path, json=correction, headers=_headers()).status_code == 200
    db.expire_all()
    corrected_details = db.query(WorkoutDetails).filter_by(record_id=event.id).one()
    assert corrected_details.distance == Decimal(5400)
    assert corrected_details.segments == fit_segments


def test_fit_storage_failure_is_explicit_and_does_not_commit_manifest(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id = user.id
    json_path = f"/api/v1/internal/users/{user_id}/imports/garmin"
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert (
            client.post(
                json_path,
                json=_batch("activities", "get_activities_by_date", "987654", _activity(), {}),
                headers=_headers(),
            ).status_code
            == 200
        )
        with patch(
            "app.api.routes.v1.internal_garmin_imports.put_fit_file",
            side_effect=FitStorageError("unavailable"),
        ):
            response = client.put(
                f"{json_path}/activities/987654/fit",
                content=make_running_fit(),
                headers={**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"},
            )
    assert response.status_code == 503
    assert db.query(ProviderNativeRecord).filter_by(kind="activity_fit_asset").count() == 0


def test_fit_parser_failure_retains_object_and_commits_failure_manifest(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id = user.id
    json_path = f"/api/v1/internal/users/{user_id}/imports/garmin"
    expected_key = f"fit-files/garmin/{user_id}/987654/parser-failure.fit"
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert (
            client.post(
                json_path,
                json=_batch("activities", "get_activities_by_date", "987654", _activity(), {}),
                headers=_headers(),
            ).status_code
            == 200
        )
        with (
            patch("app.api.routes.v1.internal_garmin_imports.put_fit_file", return_value=expected_key),
            patch("app.api.routes.v1.internal_garmin_imports.parse_fit_file", side_effect=ValueError("bad fit")),
        ):
            response = client.put(
                f"{json_path}/activities/987654/fit",
                content=make_running_fit(),
                headers={**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"},
            )
    assert response.status_code == 200
    assert response.json()["decode_status"] == "decode_failed"
    manifest = db.query(ProviderNativeRecord).filter_by(kind="activity_fit_asset").one()
    assert manifest.payload["object_key"] == expected_key
    assert manifest.payload["decode_error_category"] == "parser_error"

    with (
        patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)),
        patch("app.api.routes.v1.internal_garmin_imports.put_fit_file", return_value=expected_key),
    ):
        retried = client.put(
            f"{json_path}/activities/987654/fit",
            content=make_running_fit(),
            headers={**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"},
        )
    assert retried.status_code == 200
    assert retried.json()["status"] == "unchanged"
    assert retried.json()["decode_status"] == "decoded"
    db.expire_all()
    retried_manifest = db.query(ProviderNativeRecord).filter_by(kind="activity_fit_asset").one()
    assert retried_manifest.payload["decode_status"] == "decoded"
    assert db.query(WorkoutDetails).one().segments


def test_fit_crc_failure_is_retained_without_projection(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    json_path = f"/api/v1/internal/users/{user.id}/imports/garmin"
    invalid_fit = bytearray(make_running_fit())
    invalid_fit[-1] ^= 0xFF
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert (
            client.post(
                json_path,
                json=_batch("activities", "get_activities_by_date", "987654", _activity(), {}),
                headers=_headers(),
            ).status_code
            == 200
        )
        with patch(
            "app.api.routes.v1.internal_garmin_imports.put_fit_file",
            return_value=f"fit-files/garmin/{user.id}/987654/invalid.fit",
        ):
            response = client.put(
                f"{json_path}/activities/987654/fit",
                content=bytes(invalid_fit),
                headers={**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"},
            )

    assert response.status_code == 200
    assert response.json()["decode_status"] == "invalid_crc"
    manifest = db.query(ProviderNativeRecord).filter_by(kind="activity_fit_asset").one()
    assert manifest.payload["decode_status"] == "invalid_crc"
    assert db.query(WorkoutDetails).one().segments == []


def test_changed_fit_decode_failure_clears_superseded_fit_details(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id = user.id
    json_path = f"/api/v1/internal/users/{user_id}/imports/garmin"
    fit_path = f"{json_path}/activities/987654/fit"
    fit_headers = {**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"}
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert (
            client.post(
                json_path,
                json=_batch("activities", "get_activities_by_date", "987654", _activity(), {}),
                headers=_headers(),
            ).status_code
            == 200
        )
        with patch(
            "app.api.routes.v1.internal_garmin_imports.put_fit_file",
            return_value=f"fit-files/garmin/{user_id}/987654/valid.fit",
        ):
            assert client.put(fit_path, content=make_running_fit(), headers=fit_headers).status_code == 200

        details = db.query(WorkoutDetails).one()
        assert details.segments

        with (
            patch(
                "app.api.routes.v1.internal_garmin_imports.put_fit_file",
                return_value=f"fit-files/garmin/{user_id}/987654/changed.fit",
            ),
            patch(
                "app.api.routes.v1.internal_garmin_imports.parse_fit_file",
                side_effect=RuntimeError("parser unavailable"),
            ),
        ):
            response = client.put(fit_path, content=make_running_fit() + b"changed", headers=fit_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert response.json()["decode_status"] == "decode_failed"
    db.refresh(details)
    assert details.segments == []
    assert details.hr_zones is None
    assert details.power_zones is None


def test_purge_removes_database_records_and_fit_prefix(
    client: TestClient,
    db: Session,
    user: User,
) -> None:
    user_id = user.id
    json_path = f"/api/v1/internal/users/{user_id}/imports/garmin"
    with patch.object(settings, "garmin_bridge_ingest_secret", SecretStr(BRIDGE_SECRET)):
        assert (
            client.post(
                json_path,
                json=_batch("activities", "get_activities_by_date", "987654", _activity(), {}),
                headers=_headers(),
            ).status_code
            == 200
        )
        with patch(
            "app.api.routes.v1.internal_garmin_imports.put_fit_file",
            return_value=f"fit-files/garmin/{user_id}/987654/source.fit",
        ):
            assert (
                client.put(
                    f"{json_path}/activities/987654/fit",
                    content=make_running_fit(),
                    headers={**_headers("application/vnd.ant.fit"), "X-Garmin-Contract-Version": "1"},
                ).status_code
                == 200
            )
        db.add(
            HealthScore(
                id=uuid4(),
                user_id=user_id,
                provider=ProviderName.INTERNAL,
                category=HealthScoreCategory.RESILIENCE,
                value=Decimal("12.3"),
                recorded_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
            )
        )
        db.flush()
        with patch(
            "app.api.routes.v1.internal_garmin_imports.purge_fit_prefix",
            return_value=1,
        ) as delete_prefix:
            response = client.delete(json_path, headers=_headers())

    assert response.status_code == 200
    assert response.json()["fit_objects_deleted"] == 1
    delete_prefix.assert_called_once_with("garmin", str(user_id), required=True)
    assert db.query(ProviderNativeRecord).count() == 0
    assert db.query(DataSource).filter_by(user_id=user_id, provider=ProviderName.GARMIN).count() == 0
    assert db.query(EventRecord).count() == 0
    assert db.query(WorkoutDetails).count() == 0
    assert db.query(HealthScore).filter_by(category=HealthScoreCategory.RESILIENCE).count() == 0


def test_official_garmin_entry_points_are_unavailable(
    client: TestClient,
    user: User,
    api_key_header: dict[str, str],
) -> None:
    user_id = user.id
    assert client.get(f"/api/v1/oauth/garmin/authorize?user_id={user_id}").status_code == 404
    assert client.get("/api/v1/oauth/garmin/callback?error=access_denied").status_code == 404
    assert client.post("/api/v1/providers/garmin/webhooks", json={}).status_code == 404
    assert client.post("/api/v1/garmin/webhooks/push", json={}).status_code == 404
    assert client.post(f"/api/v1/providers/garmin/users/{user_id}/sync", headers=api_key_header).status_code == 404
    assert (
        client.get(
            f"/api/v1/providers/garmin/users/{user_id}/backfill/status",
            headers=api_key_header,
        ).status_code
        == 404
    )
