import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.database import DbSession
from app.integrations.celery.tasks import (
    sync_vendor_data,
)
from app.schemas.enums import ProviderName
from app.services import ApiKeyDep
from app.services.providers.factory import ProviderFactory
from app.utils.exceptions import UnsupportedProviderError
from app.utils.sync_params import build_sync_params

logger = logging.getLogger(__name__)

router = APIRouter()
factory = ProviderFactory()
DEFAULT_HISTORICAL_DAYS = 90


def _queue_pull_sync(
    user_id: UUID,
    provider_value: str,
    start_date: str | None,
    end_date: str | None,
    *,
    is_historical: bool = False,
) -> Any:
    """Enqueue a pull-API sync task and return the Celery AsyncResult."""
    return sync_vendor_data.delay(
        user_id=str(user_id),
        start_date=start_date,
        end_date=end_date,
        providers=[provider_value],
        is_historical=is_historical,
    )


class SyncDataType(str, Enum):
    """Types of data to sync from provider."""

    WORKOUTS = "workouts"
    DATA_247 = "247"  # Sleep, recovery, activity samples
    ALL = "all"


@router.post("/{provider}/users/{user_id}/sync")
def sync_user_data(
    provider: Annotated[ProviderName, Path(description="Data provider")],
    user_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
    # Data type selection
    data_type: Annotated[
        SyncDataType,
        Query(description="Type of data to sync: workouts, 247 (sleep/recovery/activity), or all"),
    ] = SyncDataType.ALL,
    # Suunto-specific parameters
    since: Annotated[
        int,
        Query(description="Unix timestamp to synchronize data since (0 = all, Suunto only)"),
    ] = 0,
    limit: Annotated[
        int,
        Query(description="Maximum number of items (Suunto: max 100)", le=100),
    ] = 50,
    offset: Annotated[int, Query(description="Offset for pagination (Suunto only)")] = 0,
    filter_by_modification_time: Annotated[
        bool,
        Query(description="Filter by modification time instead of creation time (Suunto only)"),
    ] = True,
    # Polar-specific parameters
    samples: Annotated[bool, Query(description="Synchronize sample data (Polar only)")] = False,
    zones: Annotated[bool, Query(description="Synchronize zones data (Polar only)")] = False,
    route: Annotated[bool, Query(description="Synchronize route data (Polar only)")] = False,
    # Deprecated Garmin-only parameters retained for client compatibility.
    summary_start_time: Annotated[
        str | None,
        Query(description="Deprecated; Garmin sync is unavailable in this fork"),
    ] = None,
    summary_end_time: Annotated[
        str | None,
        Query(description="Deprecated; Garmin sync is unavailable in this fork"),
    ] = None,
    # Async mode - dispatch to Celery worker instead of blocking
    run_async: Annotated[
        bool,
        Query(
            alias="async",
            description="Run sync asynchronously via Celery (default: true). Set false for sync.",
        ),
    ] = True,
) -> dict[str, bool | dict | str]:
    """
    Synchronize data from fitness provider API for a specific user.

    **Data Types:**
    - `workouts`: Workouts/exercises/activities
    - `247`: 24/7 data including sleep, recovery, and activity samples
    - `all`: All available data types

    **Provider-specific:**
    - **Suunto**: Supports workouts and 247 data with pagination
    - **Polar**: Supports workouts (exercises) only
    - **Garmin**: Unavailable here; the private bridge owns Garmin sync
    - **Whoop**: Supports workouts and 247 data (sleep/recovery)

    **Execution Mode:**
    - `async=true` (default): Dispatches sync to background Celery worker. Returns immediately with task ID.
    - `async=false`: Executes synchronously (may timeout for large data sets).

    Requires valid API key and active connection for the user.
    """
    if provider == ProviderName.GARMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Garmin sync is owned by garmin-bridge")

    if run_async:
        # The async worker (sync_vendor_data) always syncs all data types and
        # does not accept per-type or provider-specific flags. Reject requests
        # that would silently be ignored by the task.
        non_default_params = {
            "data_type": data_type != SyncDataType.ALL,
            "since": since != 0,
            "limit": limit != 50,
            "offset": offset != 0,
            "filter_by_modification_time": not filter_by_modification_time,
            "samples": samples,
            "zones": zones,
            "route": route,
        }
        unsupported = [k for k, v in non_default_params.items() if v]
        if unsupported:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Parameters {unsupported} are not supported in async mode. "
                    "Use async=false or omit provider-specific parameters."
                ),
            )

        start_date_iso: str | None = None
        if since > 0:
            start_date_iso = datetime.fromtimestamp(since).isoformat()
        elif summary_start_time:
            start_date_iso = summary_start_time

        task = _queue_pull_sync(user_id, provider.value, start_date_iso, summary_end_time)
        return {
            "success": True,
            "async": True,
            "task_id": task.id,
            "message": f"Sync task queued for {provider.value}. Check task status for results.",
        }

    # Synchronous mode
    strategy = factory.get_provider(provider.value)

    results: dict[str, Any] = {}

    # Collect all parameters. start_date/end_date come from build_sync_params so
    # this route and the async Celery task never drift on parameter naming.
    params: dict[str, Any] = {
        "since": since,
        "limit": limit,
        "offset": offset,
        "filter_by_modification_time": filter_by_modification_time,
        "samples": samples,
        "zones": zones,
        "route": route,
        **build_sync_params(summary_start_time, summary_end_time),
    }

    # Sync workouts if requested
    if data_type in (SyncDataType.WORKOUTS, SyncDataType.ALL):
        if strategy.workouts:
            results["workouts"] = strategy.workouts.load_data(db, user_id, **params)
        elif data_type == SyncDataType.WORKOUTS:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=f"Provider '{provider.value}' does not support workouts",
            )

    if data_type in (SyncDataType.DATA_247, SyncDataType.ALL):
        if strategy.data_247:
            load_fn = getattr(strategy.data_247, "load_and_save_all", None) or getattr(
                strategy.data_247, "load_all_247_data", None
            )
            if load_fn is None:
                results["data_247"] = None
            else:
                start_dt = datetime.fromtimestamp(since) if since else datetime.now() - timedelta(days=30)
                end_dt = datetime.now()
                results["data_247"] = load_fn(db, user_id, start_time=start_dt, end_time=end_dt)
        elif data_type == SyncDataType.DATA_247:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=f"Provider '{provider.value}' does not support 247 data (sleep/recovery/activity)",
            )

    if not results:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Provider '{provider.value}' does not support any requested data types",
        )

    return {"success": all(results.values()), "details": results}


# =============================================================================
# Historical Sync — user-initiated, provider-agnostic
# =============================================================================


@router.post("/{provider}/users/{user_id}/sync/historical")
def sync_historical_data(
    provider: Annotated[ProviderName, Path(description="Data provider")],
    user_id: UUID,
    _api_key: ApiKeyDep,
    days: Annotated[
        int,
        Query(
            description="Days of historical data to fetch (default: 90, max: 365). "
            "Garmin history is owned by the private bridge.",
            ge=1,
            le=365,
        ),
    ] = DEFAULT_HISTORICAL_DAYS,
) -> dict[str, Any]:
    """Trigger a historical sync of the user's data from a connected provider.

    Each supported provider strategy decides how to dispatch the sync. Garmin
    is unavailable here because its private bridge owns history and cursor
    state.

    **Automatic historical sync on connect (grace period)**

    v0.4.2 introduced this endpoint as the canonical, opt-in way to
    backfill historical data - the long-term goal is that connecting a
    provider only sets up live sync, and history is pulled on demand.

    To make migration painless, the pre-0.4.2 behaviour is kept for now
    behind a grace-period flag (``HISTORICAL_SYNC_ON_CONNECT``, default:
    ``true``): a historical sync is auto-dispatched after a successful OAuth
    callback for supported official providers.

    Once your integration calls this endpoint explicitly, set
    ``HISTORICAL_SYNC_ON_CONNECT=false``. The flag will default to
    ``false`` in a future release and is planned for removal afterwards.
    """
    if provider == ProviderName.GARMIN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Garmin sync is owned by garmin-bridge")
    strategy = factory.get_provider(provider.value)

    try:
        result = strategy.start_historical_sync(user_id, days)
    except UnsupportedProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.detail,
        )

    return {
        "success": True,
        "provider": provider.value,
        "user_id": str(user_id),
        "method": result.method,
        "task_id": result.task_id,
        "message": result.message,
        **({"days": result.days} if result.days is not None else {}),
        **({"start_date": result.start_date} if result.start_date is not None else {}),
        **({"end_date": result.end_date} if result.end_date is not None else {}),
    }
