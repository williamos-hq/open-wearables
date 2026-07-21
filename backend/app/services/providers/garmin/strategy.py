from app.services.providers.base_strategy import (
    BaseProviderStrategy,
    ProviderCapabilities,
    ProviderCoverage,
)
from app.services.providers.garmin.coverage import (
    HEALTH_SCORES,
    MENSTRUAL_CYCLE_FIELDS,
    SLEEP_FIELDS,
    TIMESERIES,
    WORKOUT_FIELDS,
)
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth


class GarminStrategy(BaseProviderStrategy):
    """Garmin metadata for an import-only provider.

    Official OAuth, workout, and webhook components intentionally remain unset,
    making provider network I/O unavailable through generic application paths.
    """

    def create_normalizer(self) -> Garmin247Data:
        """Build the retained upstream normalizer without exposing an I/O component."""
        oauth = GarminOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        return Garmin247Data(
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=oauth,
        )

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
            menstrual_cycle_fields=MENSTRUAL_CYCLE_FIELDS,
            health_scores=HEALTH_SCORES,
        )
