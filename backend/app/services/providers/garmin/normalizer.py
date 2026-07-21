"""Pure Garmin normalization adapter for bridge imports."""

import logging

from app.schemas.enums import ProviderName
from app.services.providers.garmin.data_247 import Garmin247Data


class GarminNormalizer(Garmin247Data):
    """Reuse upstream Garmin transformations without provider I/O or repositories."""

    def __init__(self) -> None:
        # Garmin247Data.__init__ wires OAuth clients and persistence repositories.
        # Bridge imports need only its pure normalize/build methods.
        self.provider_name = ProviderName.GARMIN.value
        self.logger = logging.getLogger(self.__class__.__name__)
