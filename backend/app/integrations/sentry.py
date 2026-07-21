import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.scrubber import EventScrubber

from app import __version__
from app.config import settings


def init_sentry() -> None:
    if settings.SENTRY_ENABLED:
        release = f"{__version__}+{settings.GIT_SHA[:12]}" if settings.GIT_SHA else __version__
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.SENTRY_ENV,
            server_name=settings.SENTRY_SERVER_NAME,
            release=release,
            traces_sample_rate=settings.SENTRY_SAMPLES_RATE,
            send_default_pii=False,
            max_request_body_size="never",
            include_local_variables=False,
            event_scrubber=EventScrubber(recursive=True),
            integrations=[
                CeleryIntegration(
                    monitor_beat_tasks=True,
                    propagate_traces=True,
                ),
            ],
        )
