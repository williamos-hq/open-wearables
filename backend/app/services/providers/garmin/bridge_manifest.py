from dataclasses import dataclass


@dataclass(frozen=True)
class GarminBridgeEndpoint:
    kind: str
    source_method: str
    normalization: str | None
    cadence: str
    key_rule: str
    expected_no_data: str
    request_policy: str = "one_local_date"
    max_records: int = 50


_ENDPOINTS = (
    GarminBridgeEndpoint("sleeps", "get_sleep_data", "sleeps", "daily", "sleep_id", "empty_object"),
    GarminBridgeEndpoint("dailies", "get_stats", "dailies", "daily", "profile_date", "empty_object"),
    GarminBridgeEndpoint("dailies", "get_user_summary", "dailies", "daily", "profile_date", "empty_object"),
    GarminBridgeEndpoint("heart_rates", "get_heart_rates", "dailies", "daily", "profile_date", "empty_object"),
    GarminBridgeEndpoint("hrv", "get_hrv_data", "hrv", "daily", "profile_date", "null"),
    GarminBridgeEndpoint("pulse_ox", "get_spo2_data", "pulse_ox", "daily", "profile_date", "empty_object"),
    GarminBridgeEndpoint("respiration", "get_respiration_data", "respiration", "daily", "profile_date", "empty_object"),
    GarminBridgeEndpoint(
        "body_compositions",
        "get_body_composition",
        "body_compositions",
        "daily",
        "measurement_timestamp_sequence",
        "empty_list",
    ),
    GarminBridgeEndpoint(
        "stress_details",
        "get_stress_data",
        "stress_details",
        "daily",
        "profile_date",
        "empty_object",
    ),
    GarminBridgeEndpoint(
        "body_battery",
        "get_body_battery",
        "stress_details",
        "daily",
        "profile_date_sequence",
        "empty_list",
    ),
    GarminBridgeEndpoint(
        "blood_pressures",
        "get_blood_pressure",
        "blood_pressures",
        "daily",
        "measurement_timestamp_sequence",
        "empty_list",
    ),
    GarminBridgeEndpoint(
        "skin_temperature",
        "get_skin_temperature",
        "skin_temperature",
        "daily",
        "profile_date",
        "empty_object",
    ),
    GarminBridgeEndpoint("user_metrics", "get_max_metrics", "user_metrics", "daily", "profile_date", "empty_object"),
    GarminBridgeEndpoint(
        "fitness_age",
        "get_fitnessage_data",
        "user_metrics",
        "weekly",
        "profile_date",
        "empty_object",
    ),
    GarminBridgeEndpoint(
        "training_readiness",
        "get_training_readiness",
        "training_readiness",
        "daily",
        "profile_timestamp_sequence",
        "empty_list",
    ),
    GarminBridgeEndpoint(
        "training_status",
        "get_training_status",
        None,
        "daily",
        "profile_date",
        "empty_object",
    ),
    GarminBridgeEndpoint(
        "training_load",
        "get_training_load_focus",
        None,
        "daily",
        "profile_date",
        "empty_object",
    ),
    GarminBridgeEndpoint(
        "activities",
        "get_activities_by_date",
        "activities",
        "overlap_7d",
        "activity_id",
        "empty_list",
        "explicit_bounded_date_range",
    ),
    GarminBridgeEndpoint(
        "activity_details",
        "get_activity",
        "activity_details",
        "on_activity_change",
        "activity_id",
        "not_found",
        "one_activity_id",
    ),
    GarminBridgeEndpoint(
        "activity_laps",
        "get_activity_splits",
        None,
        "on_activity_change",
        "activity_id",
        "empty_object",
        "one_activity_id",
    ),
    GarminBridgeEndpoint(
        "activity_routes",
        "get_activity_gps_data",
        None,
        "on_activity_change",
        "activity_id",
        "empty_object",
        "one_activity_id",
    ),
    GarminBridgeEndpoint(
        "devices",
        "get_devices",
        None,
        "weekly",
        "device_id",
        "empty_list",
        "single_bounded_list",
    ),
)

GARMIN_BRIDGE_ENDPOINTS: dict[tuple[str, str], GarminBridgeEndpoint] = {
    (endpoint.kind, endpoint.source_method): endpoint for endpoint in _ENDPOINTS
}
