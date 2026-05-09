"""weather.py — engine-side weather helpers.

Lifted from agents/morning_brief.py so non-agent surfaces (the morning
view-model in particular) can fetch weather + flag at-risk phases
without importing an agent module.

NOAA Weather.gov primary, Open-Meteo fallback — both free, no API key.

Two public entry points:
  fetch_weather(lat, lng)               → forecast dict
  weather_risk_check(phases, weather, today) → list of risk dicts

Forecast URL is cached per (lat, lng) gridpoint in
.weather_cache.json at the repo root, since NOAA grid points
basically never change for a fixed job site.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NOAA_USER_AGENT = "PalmettoCustomHomes-AI/1.0 (aiwithconnor@gmail.com)"

_WEATHER_CACHE_FILE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".weather_cache.json")
)


# Outdoor / weather-sensitive phase keywords. A phase is flagged for
# weather risk only if its name matches one of these. Engine V2 will
# move this to a per-phase-template flag once the canonical model has
# weather_sensitivity per phase; until then the keyword list is the V1
# heuristic.
_SENSITIVE_KEYWORDS = (
    "concrete", "pour", "slab", "footing", "foundation",
    "framing", "roof", "roofing", "dry-in", "dryin", "dry in",
    "siding", "exterior", "window", "door", "painting", "paint",
    "excavat", "grading", "clearing", "site work", "sitework",
    "masonry", "brick", "stucco", "drywall", "insulation",
    "hvac", "mechanical", "electrical", "plumbing",
    "landscape", "pool", "deck", "porch",
)


# ---------------------------------------------------------------------------
# Internal — NOAA fetch + caching
# ---------------------------------------------------------------------------


def _noaa_get(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": NOAA_USER_AGENT,
            "Accept": "application/geo+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _load_forecast_url_cache(lat: float, lng: float) -> str | None:
    """Return cached NOAA forecast URL for this lat/lng, or None."""
    try:
        with open(_WEATHER_CACHE_FILE) as f:
            data = json.load(f)
        key = f"{lat:.4f},{lng:.4f}"
        return data.get(key)
    except Exception:
        return None


def _save_forecast_url_cache(lat: float, lng: float, url: str) -> None:
    """Cache the NOAA forecast URL — grid points rarely change."""
    try:
        try:
            with open(_WEATHER_CACHE_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        key = f"{lat:.4f},{lng:.4f}"
        data[key] = url
        with open(_WEATHER_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _fetch_noaa(lat: float, lng: float) -> dict:
    """Try NOAA with cached grid URL, retry up to 3 times."""
    result: dict = {"periods": [], "site": f"{lat},{lng}", "error": None}

    forecast_url = _load_forecast_url_cache(lat, lng)

    for attempt in range(3):
        try:
            if not forecast_url:
                # Step 1: resolve grid point (only needed once; result is cached)
                meta = _noaa_get(f"https://api.weather.gov/points/{lat},{lng}")
                forecast_url = meta["properties"]["forecast"]
                _save_forecast_url_cache(lat, lng, forecast_url)

            # Step 2: fetch forecast
            fc = _noaa_get(forecast_url)
            periods = fc["properties"]["periods"]
            result["periods"] = periods[:4]
            return result

        except Exception as e:
            result["error"] = str(e)
            forecast_url = None        # bust cache on failure; re-resolve next attempt
            _save_forecast_url_cache(lat, lng, "")
            if attempt < 2:
                time.sleep(8)          # wait 8s between retries

    return result


def _fetch_open_meteo(lat: float, lng: float) -> dict:
    """Fallback: Open-Meteo free API — fast, no key, very reliable."""
    result: dict = {"periods": [], "site": f"{lat},{lng}", "error": None}
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lng}"
            f"&daily=temperature_2m_max,temperature_2m_min,"
            f"precipitation_probability_max,windspeed_10m_max,weathercode"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&forecast_days=2&timezone=America%2FChicago"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        daily = data.get("daily", {})
        days  = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows  = daily.get("temperature_2m_min", [])
        precip= daily.get("precipitation_probability_max", [])
        wind  = daily.get("windspeed_10m_max", [])
        codes = daily.get("weathercode", [])

        # Map WMO weather codes to short descriptions
        def _wmo(code):
            if code is None:
                return "Unknown"
            c = int(code)
            if c == 0:   return "Clear Sky"
            if c <= 3:   return "Partly Cloudy"
            if c <= 9:   return "Overcast"
            if c <= 29:  return "Foggy"
            if c <= 39:  return "Drizzle"
            if c <= 49:  return "Freezing Drizzle"
            if c <= 59:  return "Rain"
            if c <= 69:  return "Freezing Rain"
            if c <= 79:  return "Snow"
            if c <= 84:  return "Rain Showers"
            if c <= 94:  return "Thunderstorm"
            return "Severe Thunderstorm"

        label_map = {0: "Today", 1: "Tomorrow"}
        for i, day in enumerate(days[:2]):
            result["periods"].append({
                "name": label_map.get(i, day),
                "temperature": int(highs[i]) if i < len(highs) else "?",
                "temperatureUnit": "F",
                "shortForecast": _wmo(codes[i] if i < len(codes) else None),
                "detailedForecast": (
                    f"High {int(highs[i])}°F, Low {int(lows[i])}°F. "
                    f"Precipitation chance {int(precip[i])}%. "
                    f"Wind up to {int(wind[i])} mph."
                ) if all(i < len(x) for x in [highs, lows, precip, wind]) else "",
                "windSpeed": f"{int(wind[i])} mph" if i < len(wind) else "?",
                "windDirection": "",
                "probabilityOfPrecipitation": {
                    "value": int(precip[i]) if i < len(precip) else 0
                },
                "_source": "open-meteo",
            })
    except Exception as e:
        result["error"] = str(e)
    return result


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def fetch_weather(lat: float, lng: float) -> dict:
    """Return today + tomorrow forecast periods.

    Tries NOAA first (3 attempts, cached grid URL). Falls back to
    Open-Meteo if NOAA fails — so the brief always has weather data.

    Returns:
        {
          "periods": [...],   # up to 4 periods (NOAA) or 2 days (Open-Meteo)
          "site": "<lat>,<lng>",
          "error": None | "<message>",
          "_source": "noaa" | "open-meteo" | "failed",
        }
    """
    result = _fetch_noaa(lat, lng)
    if result["periods"]:
        result["_source"] = "noaa"
        return result

    # NOAA failed — try Open-Meteo
    fallback = _fetch_open_meteo(lat, lng)
    if fallback["periods"]:
        fallback["_source"] = "open-meteo"
        fallback["error"] = None       # got data, don't surface the NOAA error
        return fallback

    # Both failed
    result["_source"] = "failed"
    result["error"] = (
        f"NOAA: {result['error']} | Open-Meteo: {fallback['error']}"
    )
    return result


def weather_risk_check(phases: list, weather: dict, today: date) -> list[dict]:
    """Return phases scheduled this week that may be affected by adverse weather.

    A phase is flagged when:
      - Its Start..End window overlaps the next 7 days
      - Its name suggests outdoor/weather-sensitive work
      - Forecast includes rain >= 40% OR wind >= 30 mph OR temp < 35°F OR temp > 100°F

    Accepts phases as either:
      - Tracker-shape dicts ({"Phase", "Start", "End", "Status"}) — legacy morning_brief path
      - engine.Phase objects (with .name, .planned_start_date, .planned_end_date, .status)

    Returns list of {"phase": str, "risk": str, "detail": str}.
    """
    if not weather.get("periods"):
        return []

    # Summarize weather flags from periods
    rain_pct = 0
    max_wind_mph = 0
    min_temp = 999
    max_temp = -999

    for period in weather["periods"]:
        pop = (period.get("probabilityOfPrecipitation") or {}).get("value") or 0
        rain_pct = max(rain_pct, pop)

        wind_str = period.get("windSpeed", "0 mph")
        wind_match = re.search(r"(\d+)", wind_str)
        if wind_match:
            max_wind_mph = max(max_wind_mph, int(wind_match.group(1)))

        temp = period.get("temperature", 70)
        try:
            temp = int(temp)
        except (TypeError, ValueError):
            temp = 70
        min_temp = min(min_temp, temp)
        max_temp = max(max_temp, temp)

    week_end = today + timedelta(days=7)
    risks: list[dict] = []

    for p in phases:
        phase_name, p_start, p_end, status = _extract_phase_fields(p)
        if status == "done" or status == "complete":
            continue
        if p_start is None or p_end is None:
            continue
        if p_end < today or p_start > week_end:
            continue

        name_lower = phase_name.lower()
        if not any(kw in name_lower for kw in _SENSITIVE_KEYWORDS):
            continue

        risk_parts: list[str] = []
        if rain_pct >= 40:
            risk_parts.append(f"{rain_pct}% rain chance")
        if max_wind_mph >= 30:
            risk_parts.append(f"winds to {max_wind_mph} mph")
        if min_temp < 35:
            risk_parts.append(f"low temp {min_temp}°F")
        if max_temp > 100:
            risk_parts.append(f"high temp {max_temp}°F")

        if risk_parts:
            risks.append({
                "phase": phase_name,
                "risk": ", ".join(risk_parts),
                "detail": f"Scheduled {p_start} – {p_end}",
            })

    return risks


def _extract_phase_fields(
    p,
) -> tuple[str, date | None, date | None, str]:
    """Normalize a phase representation into (name, start, end, status_lower).

    Supports both the legacy Tracker dict shape and the engine.Phase
    dataclass — so callers from either world (morning_brief.py reading
    Tracker rows, hb-morning reading engine.Schedule.phases) can use
    weather_risk_check uniformly.
    """
    # engine.Phase dataclass — duck-type via attribute presence
    if hasattr(p, "planned_start_date") and hasattr(p, "name"):
        name = p.name
        start = p.planned_start_date
        end = p.planned_end_date
        status = (p.status or "").strip().lower()
        return (name, start, end, status)

    # Tracker dict shape
    if isinstance(p, dict):
        name = p.get("Phase", "")
        status = (p.get("Status") or "").strip().lower()
        try:
            p_start = datetime.strptime(str(p.get("Start", "")).strip(), "%Y-%m-%d").date()
            p_end = datetime.strptime(str(p.get("End", "")).strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return (name, None, None, status)
        return (name, p_start, p_end, status)

    # Unknown shape — silently skip
    return ("", None, None, "")
