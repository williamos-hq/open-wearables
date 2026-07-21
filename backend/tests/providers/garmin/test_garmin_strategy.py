"""Tests for Garmin strategy."""

from app.services.providers.garmin.strategy import GarminStrategy


class TestGarminStrategy:
    """Tests for GarminStrategy class."""

    def test_name_is_garmin(self) -> None:
        """Strategy name should be 'garmin'."""
        strategy = GarminStrategy()
        assert strategy.name == "garmin"

    def test_api_base_url(self) -> None:
        """API base URL should be Garmin's API endpoint."""
        strategy = GarminStrategy()
        assert strategy.api_base_url == "https://apis.garmin.com"

    def test_display_name(self) -> None:
        """Display name should be capitalized provider name."""
        strategy = GarminStrategy()
        assert strategy.display_name == "Garmin"

    def test_has_cloud_api(self) -> None:
        """Garmin is not exposed as a cloud API in the import-only fork."""
        strategy = GarminStrategy()
        assert strategy.has_cloud_api is False

    def test_icon_url(self) -> None:
        """Icon URL should point to Garmin SVG icon."""
        strategy = GarminStrategy()
        assert strategy.icon_url == "/static/provider-icons/garmin.svg"

    def test_official_io_components_are_not_initialized(self) -> None:
        """Import-only Garmin must not expose official provider clients."""
        strategy = GarminStrategy()
        assert strategy.oauth is None
        assert strategy.workouts is None
        assert strategy.data_247 is None
        assert strategy.webhooks is None

    def test_bridge_normalizer_is_explicitly_available(self) -> None:
        normalizer = GarminStrategy().create_normalizer()
        assert normalizer.provider_name == "garmin"

    def test_repositories_initialized(self) -> None:
        """All required repositories should be initialized."""
        strategy = GarminStrategy()
        assert strategy.user_repo is not None
        assert strategy.connection_repo is not None
        assert strategy.workout_repo is not None
