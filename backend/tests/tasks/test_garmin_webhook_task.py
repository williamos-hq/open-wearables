"""Tests for the disabled official Garmin webhook task path."""

from unittest.mock import patch

import pytest
from celery.exceptions import Retry

from app.integrations.celery.tasks.webhook_push_task import process_webhook_push

MODULE = "app.integrations.celery.tasks.webhook_push_task"


def test_direct_garmin_webhook_task_is_unavailable() -> None:
    with pytest.raises(ValueError, match="Provider 'garmin' has no webhook handler"):
        process_webhook_push.run("garmin", {"activities": []}, "trace-disabled")


def test_push_retries_on_provider_factory_failure() -> None:
    retry_exc = Retry("will retry", RuntimeError("critical failure"))
    with (
        patch.object(process_webhook_push, "retry", return_value=retry_exc) as mock_retry,
        patch(f"{MODULE}.ProviderFactory") as mock_factory_cls,
    ):
        mock_factory_cls.return_value.get_provider.side_effect = RuntimeError("critical failure")
        with pytest.raises(Retry):
            process_webhook_push.run("garmin", {}, "trace-retry-push")

    mock_retry.assert_called_once()
