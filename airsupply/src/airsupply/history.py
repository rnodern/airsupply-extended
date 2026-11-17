"""Turn AirMini LoggedData notifications into per-night therapy summaries.

Pure parsing: no Bluetooth, MQTT, patient database or therapy-setting writes.
A night runs from local noon to the following noon. Device clocks may drift:
callers must supply a measured offset rather than assuming timestamps are correct.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

USAGE = "UsageEvents-TherapyStatusEvent"
RESPIRATORY = "TherapyEvents-RespiratoryEvent"
LEAK = "TherapyOneMinutePeriodic-Leak"
PRESSURE = "TherapyOneMinutePeriodic-InspiratoryPressure"
AHI_TYPES = frozenset({
    "ObstructiveApneaEnd", "CentralApneaEnd", "ApneaEnd", "HypopneaEnd",
})


def corrected_time(raw, offset):
    """Parse an ISO timestamp and apply the measured device clock offset."""
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is not None:
            return value + timedelta(seconds=offset)
    except (ValueError, OverflowError):
        return None
    return None


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    pos = fraction * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 3)


def nightly_summaries(notifications, *, timezone="Australia/Melbourne",
                      clock_offset_seconds=0):
    """Summarise LoggedData from a complete or partial history pull.

    Accepts raw libairmini notification dictionaries or saved JSONL envelopes
    with the notification under 'data'. Duplicate batches are deduplicated
    before calculating totals. Unmatched MaskOn/MaskOff events are excluded.

    Leak/pressure units are deliberately labelled *_raw: do not publish them
    as L/min or cmH2O until verified against the device/application.
    The clock offset is host UTC minus reported device UTC, in seconds.
    """
    zone = ZoneInfo(timezone)
    offset = float(clock_offset_seconds)
    if not math.isfinite(offset):
        raise ValueError("clock offset must be finite")

    usage = set()
    respiratory = set()
    periodic = {LEAK: {}, PRESSURE: {}}
    completed = set()

    for row in notifications:
        if not isinstance(row, dict):
            continue
        message = row.get("data", row)
        if not isinstance(message, dict) or message.get("method") != "LoggedData":
            continue
        params = message.get("params")
        if not isinstance(params, dict):
            continue
        for item in params.get("data", []):
            if not isinstance(item, dict):
                continue
            signal = item.get("dataId")
            if signal not in (USAGE, RESPIRATORY, LEAK, PRESSURE):
                continue
            if item.get("complete") is True:
                completed.add(signal)
            if signal in (USAGE, RESPIRATORY):
                for event in item.get("events", []):
                    if not isinstance(event, dict):
                        continue
                    instant = corrected_time(event.get("time"), offset)
                    kind = event.get("event")
                    if instant is not None and isinstance(kind, str):
                        if signal == USAGE:
                            usage.add((instant, kind))
                        else:
                            respiratory.add((instant, kind))
                continue

            record = item.get("periodic")
            if not isinstance(record, dict):
                continue
            start = corrected_time(record.get("startTime"), offset)
            if start is None:
                continue
            try:
                interval = float(record["interval"])
            except (TypeError, KeyError, ValueError):
                continue
            if not math.isfinite(interval) or interval <= 0:
                continue
            values = record.get("values", [])
            if not isinstance(values, list):
                continue
            for index, raw in enumerate(values):
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                if not math.isfinite(raw):
                    continue
                timestamp = start + timedelta(seconds=index * interval)
                periodic[signal][timestamp] = float(raw)

    sessions = []
    active = None
    # 'MaskOff' sorts before 'MaskOn' for simultaneous boundary events.
    for instant, kind in sorted(usage):
        if kind == "MaskOn":
            if active is None:
                active = instant
        elif kind == "MaskOff" and active is not None and instant > active:
            sessions.append((active, instant))
            active = None

    nights = defaultdict(lambda: {
        "sessions": 0, "seconds": 0.0, "types": Counter(),
        "leak": [], "pressure": [],
    })
    for start, end in sessions:
        night = (start.astimezone(zone) - timedelta(hours=12)).date().isoformat()
        row = nights[night]
        row["sessions"] += 1
        row["seconds"] += (end - start).total_seconds()
        row["types"].update(
            kind for instant, kind in respiratory if start <= instant < end
        )
        row["leak"].extend(
            value for instant, value in periodic[LEAK].items()
            if start <= instant < end
        )
        row["pressure"].extend(
            value for instant, value in periodic[PRESSURE].items()
            if start <= instant < end
        )

    output = []
    for night in sorted(nights):
        row = nights[night]
        types = row["types"]
        ahi_events = sum(types[kind] for kind in AHI_TYPES)
        hours = row["seconds"] / 3600
        output.append({
            "night": night,
            "sessions": row["sessions"],
            "usage_hours": round(hours, 3),
            "ahi": round(ahi_events / hours, 2) if hours else None,
            "ahi_events": ahi_events,
            "obstructive_apnoeas": types["ObstructiveApneaEnd"],
            "central_apnoeas": types["CentralApneaEnd"],
            "unclassified_apnoeas": types["ApneaEnd"],
            "hypopnoeas": types["HypopneaEnd"],
            "rera": types["ReraEnd"],
            "leak_samples": len(row["leak"]),
            "pressure_samples": len(row["pressure"]),
            "leak_median_raw": round(statistics.median(row["leak"]), 3)
            if row["leak"] else None,
            "leak_p95_raw": _percentile(row["leak"], 0.95),
            "pressure_median_raw": round(statistics.median(row["pressure"]), 3)
            if row["pressure"] else None,
            "pressure_p95_raw": _percentile(row["pressure"], 0.95),
            "complete_signals": sorted(completed),
        })
    return output
