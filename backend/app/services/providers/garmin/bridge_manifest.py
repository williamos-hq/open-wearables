import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GarminBridgeEndpoint:
    kind: str
    source_method: str
    normalization: str | None
    cadence: str
    key_rule: str
    expected_no_data: str
    request_policy: str
    max_records: int


GARMIN_BRIDGE_MANIFEST_PATH = Path(__file__).with_name("bridge_manifest_v1.json")


def _load_manifest() -> tuple[GarminBridgeEndpoint, ...]:
    raw: Any = json.loads(GARMIN_BRIDGE_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("contract_version") != 1:
        raise RuntimeError("invalid Garmin bridge manifest version")
    raw_endpoints = raw.get("endpoints")
    if not isinstance(raw_endpoints, list) or not raw_endpoints:
        raise RuntimeError("Garmin bridge manifest has no endpoints")

    endpoints: list[GarminBridgeEndpoint] = []
    for item in raw_endpoints:
        if not isinstance(item, dict):
            raise RuntimeError("invalid Garmin bridge manifest endpoint")
        try:
            endpoint = GarminBridgeEndpoint(**item)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid Garmin bridge manifest endpoint") from exc
        if not 1 <= endpoint.max_records <= 50:
            raise RuntimeError("invalid Garmin bridge manifest record limit")
        endpoints.append(endpoint)

    identities = {(endpoint.kind, endpoint.source_method) for endpoint in endpoints}
    if len(identities) != len(endpoints):
        raise RuntimeError("duplicate Garmin bridge manifest endpoint")
    return tuple(endpoints)


GARMIN_BRIDGE_ENDPOINT_LIST = _load_manifest()
GARMIN_BRIDGE_ENDPOINTS: dict[tuple[str, str], GarminBridgeEndpoint] = {
    (endpoint.kind, endpoint.source_method): endpoint for endpoint in GARMIN_BRIDGE_ENDPOINT_LIST
}
