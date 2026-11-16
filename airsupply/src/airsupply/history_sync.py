"""Opt-in, read-only AirMini history collection on AirSupply's held session."""

import datetime as dt
import os

from . import history
from .history_store import HistoryStore

SIGNALS = (history.USAGE, history.RESPIRATORY, history.LEAK, history.PRESSURE)


class HistoryManager:
    def __init__(self, *, store=None, timezone=None, clock_offset=None,
                 sync_hours=None, timeout=None):
        self.store = store or HistoryStore()
        self.timezone = timezone or os.environ.get(
            "AIRSUPPLY_HISTORY_TIMEZONE", "Australia/Melbourne"
        )
        self.clock_offset = float(
            clock_offset if clock_offset is not None
            else os.environ.get("AIRSUPPLY_HISTORY_CLOCK_OFFSET_SECONDS", "0")
        )
        self.sync_hours = max(1.0, float(
            sync_hours if sync_hours is not None
            else os.environ.get("AIRSUPPLY_HISTORY_SYNC_HOURS", "12")
        ))
        self.timeout = max(30, float(
            timeout if timeout is not None
            else os.environ.get("AIRSUPPLY_HISTORY_TIMEOUT_SECONDS", "300")
        ))

    def is_due(self, address, *, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        last = self.store.last_success(address)
        return last is None or now - last >= dt.timedelta(hours=self.sync_hours)

    async def sync(self, session, address):
        """Only call while holding AirSupply's existing per-device session lock.

        A missing completion flag or disconnect raises; nothing is persisted
        until all four streams are fully received.
        """
        now = dt.datetime.now(dt.timezone.utc)
        since = (now - dt.timedelta(days=16)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        notifications = await session.collect_logged_data(
            SIGNALS, since, timeout=self.timeout
        )
        nights = history.nightly_summaries(
            notifications, timezone=self.timezone,
            clock_offset_seconds=self.clock_offset,
        )
        self.store.save(address, nights, at=now)
        return len(notifications), len(nights)

    def recent(self, address):
        return self.store.recent(address)

    def forget(self, address):
        self.store.forget(address)
