"""Integration boundaries for import-only Garmin."""

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
    assert normalizer.api_base_url == "https://apis.garmin.com"
