"""Tests for raw payload storage backends."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from app.services import raw_payload_storage


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Reset module-level globals before each test."""
    raw_payload_storage._storage_backend = "disabled"
    raw_payload_storage._max_size_bytes = 10 * 1024 * 1024
    raw_payload_storage._s3_bucket = None
    raw_payload_storage._s3_prefix = "raw-payloads"
    raw_payload_storage._s3_client = None
    raw_payload_storage._fit_files_enabled = False


class TestConfigure:
    def test_configure_disabled(self) -> None:
        raw_payload_storage.configure("disabled", 1024)
        assert raw_payload_storage._storage_backend == "disabled"

    def test_configure_log(self) -> None:
        raw_payload_storage.configure("log", 2048)
        assert raw_payload_storage._storage_backend == "log"
        assert raw_payload_storage._max_size_bytes == 2048

    def test_configure_s3_without_bucket_falls_back_to_disabled(self) -> None:
        raw_payload_storage.configure("s3", 1024, s3_bucket=None)
        assert raw_payload_storage._storage_backend == "disabled"

    def test_configure_s3_with_bucket(self) -> None:
        mock_client = MagicMock()
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 1024, s3_bucket="my-bucket", s3_prefix="payloads")

        assert raw_payload_storage._storage_backend == "s3"
        assert raw_payload_storage._s3_bucket == "my-bucket"
        assert raw_payload_storage._s3_prefix == "payloads"
        assert raw_payload_storage._s3_client is mock_client

    def test_configure_s3_client_creation_fails(self) -> None:
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=None):
            raw_payload_storage.configure("s3", 1024, s3_bucket="my-bucket")

        assert raw_payload_storage._storage_backend == "disabled"


class TestStoreRawPayload:
    def test_disabled_is_noop(self) -> None:
        raw_payload_storage.configure("disabled", 1024)
        # Should not raise
        raw_payload_storage.store_raw_payload(source="webhook", provider="garmin", payload={"key": "val"})

    def test_payload_exceeding_max_size_is_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        raw_payload_storage.configure("log", 10)  # 10 bytes max
        raw_payload_storage.store_raw_payload(source="webhook", provider="garmin", payload={"big": "payload"})
        captured = capsys.readouterr()
        assert "raw_payload" not in captured.out

    def test_log_backend_outputs_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        raw_payload_storage.configure("log", 10 * 1024 * 1024)
        raw_payload_storage.store_raw_payload(
            source="webhook",
            provider="garmin",
            payload={"test": True},
            user_id="user-123",
            trace_id="trace-abc",
        )
        captured = capsys.readouterr()
        entry = json.loads(captured.out.strip())
        assert entry["message"] == "raw_payload"
        assert entry["source"] == "webhook"
        assert entry["provider"] == "garmin"
        assert entry["user_id"] == "user-123"
        assert entry["trace_id"] == "trace-abc"

    def test_s3_backend_uploads_payload(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.return_value = {"ETag": "test-etag"}

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket", s3_prefix="raw")

        raw_payload_storage.store_raw_payload(
            source="webhook",
            provider="garmin",
            payload={"activity": "running"},
            user_id="user-456",
            trace_id="trace-xyz",
        )

        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert "garmin/webhook/" in call_kwargs["Key"]
        assert "/user-456/" in call_kwargs["Key"]
        assert call_kwargs["Key"].endswith(".json")
        assert call_kwargs["ContentType"] == "application/json"
        assert call_kwargs["Metadata"]["provider"] == "garmin"
        assert call_kwargs["Metadata"]["user_id"] == "user-456"
        assert call_kwargs["Metadata"]["trace_id"] == "trace-xyz"

        body = call_kwargs["Body"].decode("utf-8")
        assert json.loads(body) == {"activity": "running"}

    def test_s3_backend_handles_upload_error(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.side_effect = Exception("S3 error")

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket")

        # Should not raise - errors are logged
        raw_payload_storage.store_raw_payload(source="sdk", provider="apple", payload="raw-xml-data")

    def test_s3_backend_unknown_user_fallback(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.return_value = {"ETag": "test-etag"}

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket")

        raw_payload_storage.store_raw_payload(source="webhook", provider="garmin", payload={"x": 1})

        call_kwargs = mock_client.put_object.call_args[1]
        assert "/_unknown/" in call_kwargs["Key"]

    def test_s3_backend_pre_serialized_string(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.return_value = {"ETag": "test-etag"}

        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure("s3", 10 * 1024 * 1024, s3_bucket="test-bucket")

        raw_payload_storage.store_raw_payload(source="sdk", provider="apple", payload='{"pre":"serialized"}')

        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Body"] == b'{"pre":"serialized"}'


class TestFitStorage:
    def test_put_is_content_addressed_private_and_encrypted(self) -> None:
        mock_client = MagicMock()
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure(
                "disabled",
                1024,
                s3_bucket="private-bucket",
                fit_files_enabled=True,
            )

        fit_bytes = b"\x0e\x20\x00\x00\x00\x00\x00\x00.FITsource"
        digest = hashlib.sha256(fit_bytes).hexdigest()
        key = raw_payload_storage.put_fit_file(
            provider="garmin",
            fit_bytes=fit_bytes,
            user_id="user-123",
            activity_id="987654",
        )

        assert key == f"fit-files/garmin/user-123/987654/{digest}.fit"
        call = mock_client.put_object.call_args.kwargs
        assert call["Bucket"] == "private-bucket"
        assert call["ContentType"] == "application/vnd.ant.fit"
        assert call["ServerSideEncryption"] == "AES256"
        assert call["Metadata"]["sha256"] == digest

    def test_put_failure_is_explicit(self) -> None:
        mock_client = MagicMock()
        mock_client.put_object.side_effect = RuntimeError("unavailable")
        with patch.object(raw_payload_storage, "_create_s3_client", return_value=mock_client):
            raw_payload_storage.configure(
                "disabled",
                1024,
                s3_bucket="private-bucket",
                fit_files_enabled=True,
            )

        with pytest.raises(raw_payload_storage.FitStorageError):
            raw_payload_storage.put_fit_file(
                provider="garmin",
                fit_bytes=b"fit",
                user_id="user-123",
                activity_id="987654",
            )

    def test_delete_prefix_paginates(self) -> None:
        mock_client = MagicMock()
        mock_client.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": "fit-files/garmin/user-123/1/a.fit"}],
                "IsTruncated": True,
                "NextContinuationToken": "next",
            },
            {
                "Contents": [{"Key": "fit-files/garmin/user-123/2/b.fit"}],
                "IsTruncated": False,
            },
        ]
        mock_client.delete_objects.return_value = {"Errors": []}
        raw_payload_storage._s3_bucket = "private-bucket"
        raw_payload_storage._s3_client = mock_client

        assert raw_payload_storage.delete_fit_prefix("garmin", "user-123") == 2
        assert mock_client.list_objects_v2.call_count == 2
        assert mock_client.delete_objects.call_count == 2

    def test_delete_prefix_rejects_partial_acknowledgement(self) -> None:
        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "fit-files/garmin/user-123/1/a.fit"}],
            "IsTruncated": False,
        }
        mock_client.delete_objects.return_value = {"Errors": [{"Key": "a.fit", "Code": "AccessDenied"}]}
        raw_payload_storage._s3_bucket = "private-bucket"
        raw_payload_storage._s3_client = mock_client

        with pytest.raises(raw_payload_storage.FitStorageError):
            raw_payload_storage.delete_fit_prefix("garmin", "user-123")

    def test_purge_prefix_requires_configuration_for_known_objects(self) -> None:
        with pytest.raises(raw_payload_storage.FitStorageError):
            raw_payload_storage.purge_fit_prefix("garmin", "user-123", required=True)

    def test_purge_prefix_is_optional_without_known_objects(self) -> None:
        assert raw_payload_storage.purge_fit_prefix("garmin", "user-123") == 0
