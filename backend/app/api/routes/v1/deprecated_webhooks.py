"""Deprecated webhook routes retained for backward compatibility.

These paths were registered in production before the unified
``/providers/{provider}/webhooks`` architecture was introduced.

New integrations must use:
  POST /api/v1/providers/{provider}/webhooks

Garmin compatibility paths are intentionally absent in this import-only fork.
Oura and Strava paths remain for backward compatibility.
"""

from fastapi import APIRouter

from .oura_webhooks import router as oura_webhooks_router
from .strava_webhooks import router as strava_webhooks_router

router = APIRouter()


# Oura and Strava — re-registered at old paths for completeness.
# These were never in production at these paths.
router.include_router(oura_webhooks_router, prefix="/oura/webhooks")
router.include_router(strava_webhooks_router, prefix="/strava/webhook")
