"""Convert Air11 device summaries into AirSupply's per-night MQTT records."""

import math
from zoneinfo import ZoneInfo

from .protocol.summary import parse_summary_spool


def nightly_summaries(payload, timezone):
    zone = ZoneInfo(timezone)
    nights = {}
    for day in parse_summary_spool(payload):
        if not isinstance(day.usage_minutes, int) or day.usage_minutes < 0:
            raise ValueError("Invalid summary duration")
        night = day.day_start.astimezone(zone).date().isoformat()
        row = {
            "night": night,
            "usage_hours": round(day.usage_minutes / 60, 6),
            "sessions": day.mask_on_count,
            "source": "airsense11_summary",
            "ahi_source": "device",
        }
        if day.ahi is not None and math.isfinite(day.ahi) and day.ahi >= 0:
            row["ahi"] = day.ahi
        # The spool reports indices. Raw event counts cannot be recovered
        # exactly from rounded indices and minutes.
        nights[night] = row
    return [nights[night] for night in sorted(nights, reverse=True)]
