#!/usr/bin/env python3
"""Open-Meteo marine forecast supplement for ISPRA stations (clearly modelled, not observed)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://marine-api.open-meteo.com/v1/marine"
HOURLY_FIELDS = [
    "wave_height", "wave_direction", "wave_period", "wave_peak_period",
    "wind_wave_height", "wind_wave_direction", "wind_wave_period", "wind_wave_peak_period",
    "swell_wave_height", "swell_wave_direction", "swell_wave_period", "swell_wave_peak_period",
    "sea_surface_temperature", "ocean_current_velocity", "ocean_current_direction",
    "sea_level_height_msl",
]


def marine_forecast(lat: float, lon: float) -> dict | None:
    """Return the closest hourly sea-state model value with explicit source and valid time."""
    params = {
        "latitude": f"{lat:.5f}",
        "longitude": f"{lon:.5f}",
        "hourly": ",".join(HOURLY_FIELDS),
        "daily": "wave_height_max",
        "forecast_days": 2,
        "timezone": "UTC",
        "wind_speed_unit": "ms",
        "temperature_unit": "celsius",
    }
    req = Request(
        API + "?" + urlencode(params),
        headers={"User-Agent": "SardegnaMeteoLive/1.0 (marine forecast attribution to Open-Meteo)"},
    )
    try:
        with urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return None
        now = datetime.now(timezone.utc)
        parsed = [datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(timezone.utc) for t in times]
        idx = min(range(len(parsed)), key=lambda i: abs((parsed[i] - now).total_seconds()))
        valid_time = parsed[idx].isoformat(timespec="seconds").replace("+00:00", "Z")

        def at(field: str):
            values = hourly.get(field) or []
            if idx >= len(values) or values[idx] is None:
                return None
            try:
                value = float(values[idx])
                return value if value == value else None
            except (TypeError, ValueError):
                return None

        daily = data.get("daily") or {}
        daily_times = daily.get("time") or []
        daily_values = daily.get("wave_height_max") or []
        day_idx = min(range(len(daily_times)), key=lambda i: abs((datetime.fromisoformat(str(daily_times[i]).replace("Z", "+00:00")).date() - now.date()).days)) if daily_times else -1
        wave_height_max_day = None
        if 0 <= day_idx < len(daily_values) and daily_values[day_idx] is not None:
            try:
                wave_height_max_day = float(daily_values[day_idx])
            except (TypeError, ValueError):
                pass

        return {
            "source": "Open-Meteo Marine API · Best Match",
            "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "valid_time": valid_time,
            "wave_height_m": at("wave_height"),
            "wave_direction_deg": at("wave_direction"),
            "wave_period_s": at("wave_period"),
            "wave_peak_period_s": at("wave_peak_period"),
            "wind_wave_height_m": at("wind_wave_height"),
            "wind_wave_direction_deg": at("wind_wave_direction"),
            "wind_wave_period_s": at("wind_wave_period"),
            "wind_wave_peak_period_s": at("wind_wave_peak_period"),
            "swell_wave_height_m": at("swell_wave_height"),
            "swell_wave_direction_deg": at("swell_wave_direction"),
            "swell_wave_period_s": at("swell_wave_period"),
            "swell_wave_peak_period_s": at("swell_wave_peak_period"),
            "sea_surface_temp_c": at("sea_surface_temperature"),
            "current_speed_ms": at("ocean_current_velocity"),
            "current_direction_deg": at("ocean_current_direction"),
            "sea_level_msl_m": at("sea_level_height_msl"),
            "wave_height_max_day_m": wave_height_max_day,
            "wave_height_max_day": daily_times[day_idx] if 0 <= day_idx < len(daily_times) else None,
        }
    except Exception as exc:
        print(f"Previsione marina non disponibile per {lat:.4f},{lon:.4f}: {exc}")
        return None
