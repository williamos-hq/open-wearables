from unittest.mock import patch

from sentry_sdk.scrubber import EventScrubber

from app.config import settings
from app.integrations.sentry import init_sentry


def test_sentry_disables_request_bodies_locals_and_default_pii() -> None:
    with (
        patch.object(settings, "SENTRY_ENABLED", True),
        patch("app.integrations.sentry.sentry_sdk.init") as init,
    ):
        init_sentry()

    kwargs = init.call_args.kwargs
    assert kwargs["send_default_pii"] is False
    assert kwargs["max_request_body_size"] == "never"
    assert kwargs["include_local_variables"] is False
    assert isinstance(kwargs["event_scrubber"], EventScrubber)
    assert kwargs["event_scrubber"].recursive is True
