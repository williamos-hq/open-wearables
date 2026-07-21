"""Pure Garmin-to-Open-Wearables normalization helpers."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.constants.sleep import SleepStageType
from app.constants.workout_types.garmin import get_unified_workout_type
from app.schemas.enums import HealthScoreCategory, ProviderName, SeriesType, daily_total_flag
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    HealthScoreCreate,
    ScoreComponent,
    SleepStage,
    TimeSeriesSampleCreate,
)
from app.services.providers.garmin.coverage import DAILIES_SERIES
from app.utils.dates import offset_to_iso
from app.utils.structured_logging import log_structured


class GarminNormalizer:
    """Normalize canonical bridge records without provider network or persistence access."""

    def __init__(self) -> None:
        self.provider_name = ProviderName.GARMIN.value
        self.logger = logging.getLogger(self.__class__.__name__)

    def _from_epoch_seconds(self, ts: int) -> datetime:
        """Convert UTC Unix timestamp (seconds) to datetime."""
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _extract_sleep_stages_from_map(self, sleep_map: dict) -> list[SleepStage]:
        """Extract sleep stage intervals from Garmin sleepLevelsMap."""
        stages: list[SleepStage] = []
        for stage_key, intervals in sleep_map.items():
            try:
                stage_type = SleepStageType(stage_key)
            except ValueError:
                continue
            for iv in intervals:
                try:
                    stages.append(
                        SleepStage(
                            stage=stage_type,
                            start_time=datetime.fromtimestamp(iv["startTimeInSeconds"], tz=timezone.utc),
                            end_time=datetime.fromtimestamp(iv["endTimeInSeconds"], tz=timezone.utc),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    log_structured(
                        self.logger,
                        "warning",
                        "Invalid sleep stage interval data",
                        interval_data=iv,
                        provider="garmin",
                        task="extract_sleep_stages_from_map",
                    )
                    continue

        return sorted(stages, key=lambda s: s.start_time)

    def _normalize_sleep_health_score(
        self,
        normalized_sleep: dict[str, Any],
        user_id: UUID,
    ) -> HealthScoreCreate | None:
        """Extract sleep health score from a normalized Garmin sleep record."""
        sleep_score = normalized_sleep.get("sleep_score")
        sleep_qualifier = normalized_sleep.get("sleep_qualifier")
        sleep_score_components = normalized_sleep.get("sleep_score_components")

        if sleep_score is None and sleep_qualifier is None:
            return None

        start_time_str = normalized_sleep.get("start_time")
        if not start_time_str:
            return None
        try:
            recorded_at = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

        return HealthScoreCreate(
            id=uuid4(),
            user_id=user_id,
            provider=ProviderName.GARMIN,
            category=HealthScoreCategory.SLEEP,
            value=sleep_score,
            qualifier=sleep_qualifier,
            recorded_at=recorded_at,
            components=sleep_score_components,
        )

    def normalize_sleep(
        self,
        raw_sleep: dict[str, Any],
        user_id: UUID,
    ) -> tuple[dict[str, Any], HealthScoreCreate | None]:
        """Normalize Garmin sleep data to internal schema."""
        start_ts = raw_sleep.get("startTimeInSeconds", 0)
        duration = raw_sleep.get("durationInSeconds", 0)
        end_ts = start_ts + duration

        start_dt = self._from_epoch_seconds(start_ts)
        end_dt = self._from_epoch_seconds(end_ts)
        zone_offset = offset_to_iso(raw_sleep.get("startTimeOffsetInSeconds"))

        # Sleep stages (in seconds)
        deep_seconds = raw_sleep.get("deepSleepDurationInSeconds") or 0
        light_seconds = raw_sleep.get("lightSleepDurationInSeconds") or 0
        rem_seconds = raw_sleep.get("remSleepInSeconds") or 0
        awake_seconds = raw_sleep.get("awakeDurationInSeconds") or 0

        sleep_stages: list[SleepStage] | None = None
        if sleep_map := raw_sleep.get("sleepLevelsMap"):
            sleep_stages = self._extract_sleep_stages_from_map(sleep_map)
            if sleep_stages:
                last_stage_end = max(int(s.end_time.timestamp()) for s in sleep_stages)
                end_ts = max(end_ts, last_stage_end)
                duration = end_ts - start_ts
                end_dt = self._from_epoch_seconds(end_ts)

        # Extract sleep score if available
        sleep_score = None
        sleep_qualifier = None
        overall_score = raw_sleep.get("overallSleepScore")
        if overall_score and isinstance(overall_score, dict):
            sleep_score = overall_score.get("value")
            sleep_qualifier = overall_score.get("qualifier")

        sleep_scores = raw_sleep.get("sleepScores") or {}
        sleep_score_components = {
            key: ScoreComponent(qualifier=value.get("qualifierKey")) for key, value in sleep_scores.items()
        }

        normalized = {
            "id": uuid4(),
            "user_id": user_id,
            "provider": self.provider_name,
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "zone_offset": zone_offset,
            "duration_seconds": duration,
            "stages": {
                "deep_seconds": deep_seconds,
                "light_seconds": light_seconds,
                "rem_seconds": rem_seconds,
                "awake_seconds": awake_seconds,
            },
            "stage_timestamps": sleep_stages,
            "sleep_score": sleep_score,
            "sleep_qualifier": sleep_qualifier,
            "sleep_score_components": sleep_score_components,
            "validation": raw_sleep.get("validation"),
            "garmin_summary_id": raw_sleep.get("summaryId"),
            "is_nap": False,
            "naps": raw_sleep.get("naps") or [],
            "raw": raw_sleep,
        }
        return normalized, self._normalize_sleep_health_score(normalized, user_id)

    def _build_sleep_record(
        self,
        user_id: UUID,
        normalized_sleep: dict[str, Any],
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate] | None:
        """Build EventRecord + EventRecordDetail for a sleep session (no DB interaction)."""
        sleep_id = normalized_sleep["id"]

        # Parse start and end times
        start_dt = None
        end_dt = None
        if normalized_sleep.get("start_time"):
            start_dt = datetime.fromisoformat(normalized_sleep["start_time"].replace("Z", "+00:00"))
        if normalized_sleep.get("end_time"):
            end_dt = datetime.fromisoformat(normalized_sleep["end_time"].replace("Z", "+00:00"))

        if not start_dt or not end_dt:
            log_structured(
                self.logger,
                "warning",
                f"Missing start/end time for sleep {sleep_id}",
                provider="garmin",
                task="build_sleep_record",
                user_id=str(user_id),
            )
            return None

        record = EventRecordCreate(
            id=sleep_id,
            category="sleep",
            type="sleep_session",
            source_name="Garmin",
            device_model=None,
            duration_seconds=normalized_sleep.get("duration_seconds"),
            start_datetime=start_dt,
            end_datetime=end_dt,
            zone_offset=normalized_sleep.get("zone_offset"),
            external_id=normalized_sleep.get("garmin_summary_id"),
            source=self.provider_name,
            user_id=user_id,
        )

        stages = normalized_sleep.get("stages", {})
        asleep_seconds = stages.get("deep_seconds", 0) + stages.get("light_seconds", 0) + stages.get("rem_seconds", 0)
        time_in_bed_seconds = normalized_sleep.get("duration_seconds") or 0
        efficiency_score = Decimal(str(asleep_seconds / time_in_bed_seconds * 100)) if time_in_bed_seconds > 0 else None

        detail = EventRecordDetailCreate(
            record_id=sleep_id,
            sleep_total_duration_minutes=asleep_seconds // 60,
            sleep_time_in_bed_minutes=time_in_bed_seconds // 60,
            sleep_efficiency_score=efficiency_score,
            sleep_deep_minutes=stages.get("deep_seconds", 0) // 60,
            sleep_light_minutes=stages.get("light_seconds", 0) // 60,
            sleep_rem_minutes=stages.get("rem_seconds", 0) // 60,
            sleep_awake_minutes=stages.get("awake_seconds", 0) // 60,
            is_nap=normalized_sleep.get("is_nap", False),
            sleep_stages=normalized_sleep.get("stage_timestamps"),
        )

        return record, detail

    def _normalize_dailies_health_scores(
        self,
        normalized_daily: dict[str, Any],
        user_id: UUID,
    ) -> list[HealthScoreCreate]:
        """Extract stress and body battery health scores from a normalized Garmin daily."""
        scores: list[HealthScoreCreate] = []
        start_ts = normalized_daily.get("start_time_seconds")
        calendar_date = normalized_daily.get("calendar_date")
        if start_ts:
            recorded_at = self._from_epoch_seconds(start_ts)
        elif calendar_date:
            try:
                recorded_at = datetime.strptime(calendar_date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
            except ValueError:
                return scores
        else:
            return scores

        avg_stress = normalized_daily.get("avg_stress")
        raw_qualifier = normalized_daily.get("stress_qualifier")
        stress_qualifier = raw_qualifier.replace("_", " ").title() if raw_qualifier else None
        # averageStressLevel is -1 when there is not enough data to calculate
        if avg_stress is not None and avg_stress != -1:
            scores.append(
                HealthScoreCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.GARMIN,
                    category=HealthScoreCategory.STRESS,
                    value=avg_stress,
                    qualifier=stress_qualifier,
                    recorded_at=recorded_at,
                )
            )

        return scores

    def normalize_dailies(
        self,
        raw_daily: dict[str, Any],
        user_id: UUID,
    ) -> tuple[dict[str, Any], list[HealthScoreCreate]]:
        """Normalize Garmin daily summary to internal schema."""
        active_seconds = raw_daily.get("activeTimeInSeconds")
        normalized = {
            "user_id": user_id,
            "calendar_date": raw_daily.get("calendarDate"),
            "start_time_seconds": raw_daily.get("startTimeInSeconds"),
            "zone_offset": offset_to_iso(raw_daily.get("startTimeOffsetInSeconds")),
            "steps": raw_daily.get("steps"),
            "distance_meters": raw_daily.get("distanceInMeters"),
            "active_calories": raw_daily.get("activeKilocalories"),
            "bmr_calories": raw_daily.get("bmrKilocalories"),
            "floors_climbed": raw_daily.get("floorsClimbed"),
            "min_heart_rate": raw_daily.get("minHeartRateInBeatsPerMinute"),
            "max_heart_rate": raw_daily.get("maxHeartRateInBeatsPerMinute"),
            "avg_heart_rate": raw_daily.get("averageHeartRateInBeatsPerMinute"),
            "resting_heart_rate": raw_daily.get("restingHeartRateInBeatsPerMinute"),
            "avg_stress": raw_daily.get("averageStressLevel"),
            "max_stress": raw_daily.get("maxStressLevel"),
            "stress_qualifier": raw_daily.get("stressQualifier"),
            "moderate_intensity_minutes": (raw_daily.get("moderateIntensityDurationInSeconds") or 0) // 60,
            "vigorous_intensity_minutes": (raw_daily.get("vigorousIntensityDurationInSeconds") or 0) // 60,
            "active_time": active_seconds // 60 if active_seconds is not None else None,
            "heart_rate_samples": raw_daily.get("timeOffsetHeartRateSamples"),
            "garmin_summary_id": raw_daily.get("summaryId"),
        }
        return normalized, self._normalize_dailies_health_scores(normalized, user_id)

    def _build_dailies_samples(
        self,
        user_id: UUID,
        normalized_daily: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from normalized daily data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        calendar_date = normalized_daily.get("calendar_date")
        start_ts = normalized_daily.get("start_time_seconds")

        if not calendar_date and not start_ts:
            return samples

        if start_ts:
            recorded_at = self._from_epoch_seconds(start_ts)
        elif calendar_date:
            try:
                recorded_at = datetime.strptime(calendar_date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
            except ValueError:
                return samples
        else:
            return samples

        zone_offset = normalized_daily.get("zone_offset")

        for field, series_type in DAILIES_SERIES:
            value = normalized_daily.get(field)
            if value is not None:
                samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        source=self.provider_name,
                        recorded_at=recorded_at,
                        zone_offset=zone_offset,
                        value=Decimal(str(value)),
                        series_type=series_type,
                        external_id=normalized_daily.get("garmin_summary_id"),
                        is_daily_total=daily_total_flag(series_type, is_daily=True),
                    )
                )

        hr_samples = normalized_daily.get("heart_rate_samples")
        if hr_samples and isinstance(hr_samples, dict):
            samples.extend(self._collect_heart_rate_samples(user_id, start_ts or 0, hr_samples, zone_offset))

        return samples

    def _collect_heart_rate_samples(
        self,
        user_id: UUID,
        base_timestamp: int,
        hr_samples: dict[str, int],
        zone_offset: str | None = None,
    ) -> list[TimeSeriesSampleCreate]:
        """Collect heart rate samples from daily summary for bulk insert.

        Args:
            user_id: User ID
            base_timestamp: Base Unix timestamp (start of day)
            hr_samples: Dict of offset_seconds -> heart_rate_bpm
            zone_offset: ISO 8601 timezone offset string

        Returns:
            List of TimeSeriesSampleCreate objects
        """
        samples: list[TimeSeriesSampleCreate] = []
        base_dt = self._from_epoch_seconds(base_timestamp) if base_timestamp else None

        if not base_dt:
            return samples

        for offset_str, hr_value in hr_samples.items():
            try:
                offset_seconds = int(offset_str)
                recorded_at = base_dt + timedelta(seconds=offset_seconds)

                sample = TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(hr_value)),
                    series_type=SeriesType.heart_rate,
                )
                samples.append(sample)
            except Exception:
                pass

        return samples

    def _build_body_comp_samples(
        self,
        user_id: UUID,
        raw_body_comp: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from body composition data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        measurement_ts = raw_body_comp.get("measurementTimeInSeconds", 0)

        if not measurement_ts:
            return samples

        recorded_at = self._from_epoch_seconds(measurement_ts)
        summary_id = raw_body_comp.get("summaryId")
        zone_offset = offset_to_iso(raw_body_comp.get("measurementTimeOffsetInSeconds"))

        # Weight (convert grams to kg)
        weight_grams = raw_body_comp.get("weightInGrams")
        if weight_grams:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(weight_grams)) / 1000,  # Convert to kg
                    series_type=SeriesType.weight,
                    external_id=summary_id,
                )
            )

        # Body fat percentage
        body_fat = raw_body_comp.get("bodyFatInPercent")
        if body_fat:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(body_fat)),
                    series_type=SeriesType.body_fat_percentage,
                    external_id=summary_id,
                )
            )

        # BMI
        bmi = raw_body_comp.get("bodyMassIndex")
        if bmi:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(bmi)),
                    series_type=SeriesType.body_mass_index,
                    external_id=summary_id,
                )
            )

        # Skeletal muscle mass (convert grams to kg)
        muscle_mass_grams = raw_body_comp.get("muscleMassInGrams")
        if muscle_mass_grams:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(muscle_mass_grams)) / 1000,  # Convert to kg
                    series_type=SeriesType.skeletal_muscle_mass,
                    external_id=summary_id,
                )
            )

        return samples

    def _build_hrv_samples(
        self,
        user_id: UUID,
        raw_hrv: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from HRV data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        start_ts = raw_hrv.get("startTimeInSeconds", 0)
        summary_id = raw_hrv.get("summaryId")
        calendar_date = raw_hrv.get("calendarDate")
        zone_offset = offset_to_iso(raw_hrv.get("startTimeOffsetInSeconds"))

        if not start_ts:
            log_structured(
                self.logger,
                "warning",
                "HRV data missing startTimeInSeconds",
                provider="garmin",
                task="build_hrv_samples",
                user_id=str(user_id),
            )
            return samples

        # Collect lastNightAvg as the main HRV value for the night
        last_night_avg = raw_hrv.get("lastNightAvg")
        if last_night_avg is not None:
            recorded_at = self._from_epoch_seconds(start_ts)
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(last_night_avg)),
                    series_type=SeriesType.heart_rate_variability_rmssd,
                    external_id=summary_id,
                )
            )
            self.logger.debug(f"Collecting HRV nightly avg={last_night_avg}ms for {calendar_date}")

        # Collect individual HRV readings from hrvValues
        hrv_values = raw_hrv.get("hrvValues", {})
        if hrv_values and isinstance(hrv_values, dict):
            for offset_str, hrv_ms in hrv_values.items():
                try:
                    offset_seconds = int(offset_str)
                    recorded_at = self._from_epoch_seconds(start_ts + offset_seconds)
                    sample = TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        source=self.provider_name,
                        recorded_at=recorded_at,
                        zone_offset=zone_offset,
                        value=Decimal(str(hrv_ms)),
                        series_type=SeriesType.heart_rate_variability_rmssd,
                        external_id=f"{summary_id}:{offset_str}" if summary_id else None,
                    )
                    samples.append(sample)
                except Exception as e:
                    self.logger.debug(f"Failed to collect HRV value at offset {offset_str}: {e}")

        return samples

    def _build_activity_record(
        self,
        user_id: UUID,
        raw_activity: dict[str, Any],
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate] | None:
        """Build EventRecord + WorkoutDetail for an activity (no DB interaction)."""
        activity_id = raw_activity.get("activityId")
        if not activity_id:
            return None

        start_ts = raw_activity.get("startTimeInSeconds", 0)
        duration = raw_activity.get("durationInSeconds", 0)

        if not start_ts:
            return None

        start_dt = self._from_epoch_seconds(start_ts)
        end_dt = self._from_epoch_seconds(start_ts + duration) if duration else start_dt
        zone_offset = offset_to_iso(raw_activity.get("startTimeOffsetInSeconds"))

        activity_type = get_unified_workout_type(raw_activity.get("activityType", "unknown"))

        record_id = uuid4()
        record = EventRecordCreate(
            id=record_id,
            category="workout",
            type=activity_type.value,
            source_name="Garmin",
            device_model=raw_activity.get("deviceName"),
            duration_seconds=duration,
            start_datetime=start_dt,
            end_datetime=end_dt,
            zone_offset=zone_offset,
            external_id=str(activity_id),
            source=self.provider_name,
            user_id=user_id,
        )

        distance = raw_activity.get("distanceInMeters")
        calories = raw_activity.get("activeKilocalories")
        avg_hr = raw_activity.get("averageHeartRateInBeatsPerMinute")
        max_hr = raw_activity.get("maxHeartRateInBeatsPerMinute")
        elevation_gain = raw_activity.get("elevationGainInMeters")
        avg_speed = raw_activity.get("averageSpeedInMetersPerSecond")
        avg_cadence = (
            raw_activity.get("averageRunCadenceInStepsPerMinute")
            or raw_activity.get("averageBikingCadenceInRevPerMinute")
            or raw_activity.get("averageSwimCadenceInStrokesPerMinute")
        )

        detail = EventRecordDetailCreate(
            record_id=record_id,
            distance=Decimal(str(distance)) if distance is not None else None,
            energy_burned=Decimal(str(calories)) if calories is not None else None,
            heart_rate_avg=Decimal(str(avg_hr)) if avg_hr is not None else None,
            heart_rate_max=max_hr,
            total_elevation_gain=Decimal(str(elevation_gain)) if elevation_gain is not None else None,
            average_speed=Decimal(str(avg_speed)) if avg_speed is not None else None,
            average_cadence=Decimal(str(avg_cadence)) if avg_cadence is not None else None,
        )

        return record, detail

    def _normalize_body_battery_health_score(
        self,
        user_id: UUID,
        raw_stress: dict[str, Any],
    ) -> HealthScoreCreate | None:
        """Extract peak body battery health score from a stressDetails record."""
        start_ts = raw_stress.get("startTimeInSeconds", 0)
        if not start_ts:
            return None
        battery_values = raw_stress.get("timeOffsetBodyBatteryValues") or raw_stress.get("bodyBatteryValues", {})
        if not battery_values or not isinstance(battery_values, dict):
            return None
        valid = [v for v in battery_values.values() if v is not None and v >= 0]
        if not valid:
            return None
        return HealthScoreCreate(
            id=uuid4(),
            user_id=user_id,
            provider=ProviderName.GARMIN,
            category=HealthScoreCategory.BODY_BATTERY,
            value=max(valid),
            recorded_at=self._from_epoch_seconds(start_ts),
            zone_offset=offset_to_iso(raw_stress.get("startTimeOffsetInSeconds")),
        )

    def _build_stress_samples(
        self,
        user_id: UUID,
        raw_stress: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build individual stress level and body battery time series samples from a stressDetails record."""
        samples: list[TimeSeriesSampleCreate] = []
        start_ts = raw_stress.get("startTimeInSeconds", 0)

        if not start_ts:
            return samples

        zone_offset = offset_to_iso(raw_stress.get("startTimeOffsetInSeconds"))

        # Stress level values (negative values are special states: off-wrist, motion, etc.)
        stress_values = raw_stress.get("timeOffsetStressLevelValues") or raw_stress.get("stressLevelValues", {})
        if stress_values and isinstance(stress_values, dict):
            for offset_str, stress_value in stress_values.items():
                try:
                    if stress_value is None or stress_value < 0:
                        continue
                    offset_seconds = int(offset_str)
                    recorded_at = self._from_epoch_seconds(start_ts + offset_seconds)
                    samples.append(
                        TimeSeriesSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            source=self.provider_name,
                            recorded_at=recorded_at,
                            zone_offset=zone_offset,
                            value=Decimal(str(stress_value)),
                            series_type=SeriesType.garmin_stress_level,
                        )
                    )
                except Exception:
                    pass

        # Body battery values
        battery_values = raw_stress.get("timeOffsetBodyBatteryValues") or raw_stress.get("bodyBatteryValues", {})
        if battery_values and isinstance(battery_values, dict):
            for offset_str, battery_value in battery_values.items():
                try:
                    if battery_value is None or battery_value < 0:
                        continue
                    offset_seconds = int(offset_str)
                    recorded_at = self._from_epoch_seconds(start_ts + offset_seconds)
                    samples.append(
                        TimeSeriesSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            source=self.provider_name,
                            recorded_at=recorded_at,
                            zone_offset=zone_offset,
                            value=Decimal(str(battery_value)),
                            series_type=SeriesType.garmin_body_battery,
                        )
                    )
                except Exception:
                    pass

        return samples

    def _build_respiration_samples(
        self,
        user_id: UUID,
        raw_respiration: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from respiration data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        start_ts = raw_respiration.get("startTimeInSeconds", 0)
        summary_id = raw_respiration.get("summaryId")

        if not start_ts:
            return samples

        zone_offset = offset_to_iso(raw_respiration.get("startTimeOffsetInSeconds"))

        # Average respiration for the period
        avg_respiration = raw_respiration.get("avgWakingRespirationValue")
        if avg_respiration:
            recorded_at = self._from_epoch_seconds(start_ts)
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(avg_respiration)),
                    series_type=SeriesType.respiratory_rate,
                    external_id=summary_id,
                )
            )

        respiration_values = raw_respiration.get("timeOffsetEpochToBreaths", {})
        if respiration_values and isinstance(respiration_values, dict):
            for offset_str, resp_value in respiration_values.items():
                try:
                    if resp_value is None or resp_value <= 0:
                        continue
                    offset_seconds = int(offset_str)
                    recorded_at = self._from_epoch_seconds(start_ts + offset_seconds)
                    samples.append(
                        TimeSeriesSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            source=self.provider_name,
                            recorded_at=recorded_at,
                            zone_offset=zone_offset,
                            value=Decimal(str(resp_value)),
                            series_type=SeriesType.respiratory_rate,
                        )
                    )
                except Exception:
                    pass

        return samples

    def _build_pulse_ox_samples(
        self,
        user_id: UUID,
        raw_pulse_ox: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from pulse ox data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        start_ts = raw_pulse_ox.get("startTimeInSeconds", 0)
        summary_id = raw_pulse_ox.get("summaryId")

        if not start_ts:
            return samples

        zone_offset = offset_to_iso(raw_pulse_ox.get("startTimeOffsetInSeconds"))

        # Average SpO2
        avg_spo2 = raw_pulse_ox.get("avgSpo2")
        if avg_spo2:
            recorded_at = self._from_epoch_seconds(start_ts)
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(avg_spo2)),
                    series_type=SeriesType.oxygen_saturation,
                    external_id=summary_id,
                )
            )

        # Individual SpO2 readings
        spo2_values = raw_pulse_ox.get("timeOffsetSpo2Values", {})
        if spo2_values and isinstance(spo2_values, dict):
            for offset_str, spo2_value in spo2_values.items():
                try:
                    if spo2_value is None or spo2_value <= 0:
                        continue
                    offset_seconds = int(offset_str)
                    recorded_at = self._from_epoch_seconds(start_ts + offset_seconds)
                    samples.append(
                        TimeSeriesSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            source=self.provider_name,
                            recorded_at=recorded_at,
                            zone_offset=zone_offset,
                            value=Decimal(str(spo2_value)),
                            series_type=SeriesType.oxygen_saturation,
                        )
                    )
                except Exception:
                    pass

        return samples

    def _build_blood_pressure_samples(
        self,
        user_id: UUID,
        raw_bp: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from blood pressure data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        # Garmin's bloodPressures webhook carries the timestamp as
        # measurementTimeInSeconds (same as bodyComps). Fall back to the
        # legacy field names for safety.
        measurement_ts = (
            raw_bp.get("measurementTimeInSeconds")
            or raw_bp.get("measurementTimestampGMT")
            or raw_bp.get("startTimeInSeconds", 0)
        )
        summary_id = raw_bp.get("summaryId")

        if not measurement_ts:
            return samples

        recorded_at = self._from_epoch_seconds(measurement_ts)

        meas = raw_bp.get("measurementTimeOffsetInSeconds")
        start = raw_bp.get("startTimeOffsetInSeconds")
        zone_offset = offset_to_iso(meas if meas is not None else start)

        # Systolic blood pressure
        systolic = raw_bp.get("systolic")
        if systolic:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(systolic)),
                    series_type=SeriesType.blood_pressure_systolic,
                    external_id=summary_id,
                )
            )

        # Diastolic blood pressure
        diastolic = raw_bp.get("diastolic")
        if diastolic:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(diastolic)),
                    series_type=SeriesType.blood_pressure_diastolic,
                    external_id=summary_id,
                )
            )

        return samples

    def _build_user_metrics_samples(
        self,
        user_id: UUID,
        raw_metrics: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from user metrics data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        calendar_date = raw_metrics.get("calendarDate")
        summary_id = raw_metrics.get("summaryId")

        if not calendar_date:
            return samples

        try:
            recorded_at = datetime.strptime(calendar_date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            return samples

        # VO2 max
        vo2_max = raw_metrics.get("vo2Max")
        if vo2_max:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    value=Decimal(str(vo2_max)),
                    series_type=SeriesType.vo2_max,
                    external_id=summary_id,
                )
            )

        # Fitness age
        fitness_age = raw_metrics.get("fitnessAge")
        if fitness_age:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    value=Decimal(str(fitness_age)),
                    series_type=SeriesType.garmin_fitness_age,
                    external_id=summary_id,
                )
            )

        return samples

    def _build_skin_temp_samples(
        self,
        user_id: UUID,
        raw_skin_temp: dict[str, Any],
    ) -> list[TimeSeriesSampleCreate]:
        """Build time series samples from skin temperature data (no DB interaction)."""
        samples: list[TimeSeriesSampleCreate] = []
        start_ts = raw_skin_temp.get("startTimeInSeconds", 0)
        summary_id = raw_skin_temp.get("summaryId")

        if not start_ts:
            return samples

        recorded_at = self._from_epoch_seconds(start_ts)
        zone_offset = offset_to_iso(raw_skin_temp.get("startTimeOffsetInSeconds"))

        skin_temp = raw_skin_temp.get("skinTemperature")
        if skin_temp is not None:
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    source=self.provider_name,
                    recorded_at=recorded_at,
                    zone_offset=zone_offset,
                    value=Decimal(str(skin_temp)),
                    series_type=SeriesType.skin_temperature,
                    external_id=summary_id,
                )
            )

        return samples
