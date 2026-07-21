from app.services.providers.base_strategy import (
    BaseProviderStrategy,
    ProviderCapabilities,
    ProviderCoverage,
)
from app.services.providers.garmin.coverage import (
    HEALTH_SCORES,
    SLEEP_FIELDS,
    TIMESERIES,
    WORKOUT_FIELDS,
)
from app.services.providers.garmin.normalizer import GarminNormalizer


class GarminStrategy(BaseProviderStrategy):
    """Garmin metadata for an import-only provider.

    Official OAuth, workout, and webhook components intentionally remain unset,
    making provider network I/O unavailable through generic application paths.
    """

    def create_normalizer(self) -> GarminNormalizer:
        """Build the retained pure normalizer."""
        return GarminNormalizer()

    @property
    def name(self) -> str:
        return "garmin"

    @property
    def api_base_url(self) -> str:
        return "https://apis.garmin.com"

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Garmin is import-only in this fork; garmin-bridge owns all upstream I/O."""
        return ProviderCapabilities()

    @property
    def has_cloud_api(self) -> bool:
        return False

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            health_scores=HEALTH_SCORES,
        )
