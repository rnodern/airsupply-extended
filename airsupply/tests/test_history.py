"""Synthetic AirMini LoggedData tests; no real patient data or credentials."""

import unittest

from airsupply.history import nightly_summaries


def notice(signal, *, events=None, periodic=None, complete=False):
    item = {"dataId": signal, "complete": complete}
    if events is not None:
        item["events"] = events
    if periodic is not None:
        item["periodic"] = periodic
    return {
        "data": {"method": "LoggedData", "params": {"data": [item]}}
    }


def event(when, kind):
    return {"time": when, "event": kind}


USAGE = "UsageEvents-TherapyStatusEvent"
EVENTS = "TherapyEvents-RespiratoryEvent"
LEAK = "TherapyOneMinutePeriodic-Leak"
PRESSURE = "TherapyOneMinutePeriodic-InspiratoryPressure"


class NightlySummaries(unittest.TestCase):
    def setUp(self):
        self.usage = notice(USAGE, events=[
            event("2026-11-05T14:00:00Z", "MaskOn"),
            event("2026-11-05T15:00:00Z", "MaskOff"),
            event("2026-11-05T16:00:00Z", "MaskOn"),
            event("2026-11-05T17:00:00Z", "MaskOff"),
            event("2026-11-05T17:10:00Z", "PowerOff"),
        ], complete=True)
        self.events = notice(EVENTS, events=[
            event("2026-11-05T14:20:00Z", "ObstructiveApneaEnd"),
            event("2026-11-05T16:20:00Z", "HypopneaEnd"),
            event("2026-11-05T16:30:00Z", "ReraEnd"),
        ], complete=True)
        self.leak = notice(LEAK, periodic={
            "startTime": "2026-11-05T14:00:00Z",
            "interval": 60.0, "values": [0.1, 0.2, 0.3],
        }, complete=True)
        self.pressure = notice(PRESSURE, periodic={
            "startTime": "2026-11-05T16:00:00Z",
            "interval": 60.0, "values": [10.0, 12.0, 14.0],
        }, complete=True)

    def test_combines_sessions_in_noon_to_noon_reporting_day(self):
        records = [self.usage, self.events, self.leak, self.pressure]
        result = nightly_summaries(records)
        self.assertEqual(1, len(result))
        night = result[0]
        self.assertEqual("2026-11-05", night["night"])
        self.assertEqual(2, night["sessions"])
        self.assertEqual(2.0, night["usage_hours"])
        self.assertEqual(1.0, night["ahi"])
        self.assertEqual(2, night["ahi_events"])
        self.assertEqual(1, night["rera"])
        self.assertEqual(3, night["leak_samples"])
        self.assertEqual(3, night["pressure_samples"])
        self.assertAlmostEqual(0.2, night["leak_median_raw"])
        self.assertAlmostEqual(13.8, night["pressure_p95_raw"])
        self.assertEqual(4, len(night["complete_signals"]))

    def test_duplicate_history_notifications_do_not_double_count(self):
        rows = [self.usage, self.events, self.leak, self.pressure]
        once = nightly_summaries(rows)
        twice = nightly_summaries(rows + rows)
        self.assertEqual(once, twice)

    def test_partial_and_unmatched_events_do_not_create_fake_sessions(self):
        rows = [notice(USAGE, events=[
            event("2026-11-05T14:00:00Z", "MaskOff"),
            event("2026-11-05T15:00:00Z", "MaskOn"),
        ])]
        self.assertEqual([], nightly_summaries(rows))

    def test_measured_clock_offset_can_change_night_at_noon(self):
        row = notice(USAGE, events=[
            event("2026-11-05T00:59:00Z", "MaskOn"),
            event("2026-11-05T01:09:00Z", "MaskOff"),
        ])
        self.assertEqual("2026-11-04", nightly_summaries([row])[0]["night"])
        self.assertEqual("2026-11-05", nightly_summaries(
            [row], clock_offset_seconds=120
        )[0]["night"])

    def test_rejects_invalid_offset(self):
        with self.assertRaises(ValueError):
            nightly_summaries([], clock_offset_seconds=float("nan"))

    def test_ignores_nontreatment_messages_and_bad_samples(self):
        rows = [self.usage, self.leak, {"data": {"method": "StreamData"}}]
        result = nightly_summaries(rows)[0]
        self.assertEqual(3, result["leak_samples"])
        self.assertIsNone(result["pressure_median_raw"])


if __name__ == "__main__":
    unittest.main()
