"""Integration boundaries for import-only Garmin."""

from uuid import uuid4

from app.services.providers.garmin.strategy import GarminStrategy


def test_official_garmin_components_are_unavailable() -> None:
    strategy = GarminStrategy()

    assert strategy.name == "garmin"
    assert strategy.display_name == "Garmin"
    assert strategy.has_cloud_api is False
    assert strategy.oauth is None
    assert strategy.workouts is None
    assert strategy.data_247 is None
    assert strategy.webhooks is None


def test_retained_normalizer_is_created_explicitly() -> None:
    normalizer = GarminStrategy().create_normalizer()

    assert normalizer.provider_name == "garmin"


def test_retained_normalizer_uses_shared_workout_taxonomy() -> None:
    normalizer = GarminStrategy().create_normalizer()
    result = normalizer._build_activity_record(
        user_id=uuid4(),
        raw_activity={
            "activityId": 123,
            "activityType": "TRAIL_RUNNING",
            "startTimeInSeconds": 1_784_462_400,
            "durationInSeconds": 1_800,
        },
    )

    assert result is not None
    event, _ = result
    assert event.type == "trail_running"
