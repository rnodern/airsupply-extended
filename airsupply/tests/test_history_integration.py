"""Tests for read-only history collection and persistent summaries.

No live Bluetooth connection, pairing keys or personal therapy data.
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from airsupply.airmini.session import Session
from airsupply.controller import Controller
from airsupply.history_store import HistoryStore
from airsupply.history_sync import HistoryManager
from airsupply import entities, history


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_multiple_notifications_until_all_complete(self):
        session = Session(-1)
        sent = []

        async def fake_get(signals, since):
            sent.append((signals, since))
            async def simulated_messages():
                await asyncio.sleep(0)
                session._on_notify(None, b"LoggedData",
                    b'{"method":"LoggedData","params":{"data":[{"dataId":"s1","events":[],"complete":true}]}}')
                session._on_notify(None, b"LoggedData",
                    b'{"method":"LoggedData","params":{"data":[{"dataId":"s2","events":[],"complete":true}]}}')
            asyncio.create_task(simulated_messages())

        session.get_logged_data = fake_get
        rows = await session.collect_logged_data(["s1", "s2"], "2026-11-01T00:00:00Z", timeout=1)
        self.assertEqual(2, len(rows))
        self.assertEqual(1, len(sent))
        self.assertIsNone(session._history_collector)

    async def test_times_out_and_cleans_up_partial_collection(self):
        session = Session(-1)
        async def fake_get(signals, since):
            return {}
        session.get_logged_data = fake_get
        with self.assertRaises(asyncio.TimeoutError):
            await session.collect_logged_data(["s1"], "2026-11-01T00:00:00Z", timeout=0.01)
        self.assertIsNone(session._history_collector)


class StorageTests(unittest.TestCase):
    def test_upsert_reopen_and_erase_is_machine_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "history.sqlite3")
            storage = HistoryStore(path)
            fake = {"night": "2026-11-05", "ahi": 2.5, "usage_hours": 4.0}
            storage.save("AA:BB", [fake])
            storage.save("aa:bb", [{**fake, "ahi": 3.0}])
            storage.save("CC:DD", [fake])
            self.assertEqual(1, len(storage.recent("AA:BB")))
            self.assertEqual(3.0, HistoryStore(path).recent("AA:BB")[0]["ahi"])
            self.assertIsNotNone(storage.last_success("aa:bb"))
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
            storage.forget("AA:BB")
            self.assertEqual([], storage.recent("AA:BB"))
            self.assertEqual(1, len(storage.recent("CC:DD")))

    def test_history_manager_does_not_save_on_incomplete_pull(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                manager = HistoryManager(store=HistoryStore(os.path.join(folder, "h.db")))
                class Incomplete:
                    async def collect_logged_data(self, signals, since, timeout):
                        raise asyncio.TimeoutError
                with self.assertRaises(asyncio.TimeoutError):
                    await manager.sync(Incomplete(), "AA:BB")
                self.assertIsNone(manager.store.last_success("AA:BB"))
        asyncio.run(scenario())


class StandbyHistoryTests(unittest.IsolatedAsyncioTestCase):
    address = "AA:BB"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        storage = HistoryStore(os.path.join(folder.name, "history.sqlite3"))
        self.manager = HistoryManager(store=storage, timezone="UTC", clock_offset=0, sync_hours=12)

        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.end = self.now - timedelta(minutes=10)
        start = self.end - timedelta(hours=4)
        self.night = (start - timedelta(hours=12)).date().isoformat()
        previous_date = (start - timedelta(hours=36)).date().isoformat()
        self.prior = {"night": previous_date, "usage_hours": 3.0, "ahi": 0.0}
        storage.save(self.address, [self.prior], at=self.now - timedelta(hours=3))

        self.enterContext(patch("airsupply.controller.store.selected", return_value=None))
        self.enterContext(patch("airsupply.controller.store.machines", return_value={}))
        with patch.dict(os.environ, {"AIRSUPPLY_HISTORY_ENABLED": "0"}):
            self.controller = Controller(None, "test")
        self.controller.history = self.manager
        self.controller.publisher = Mock()
        self.session = AsyncMock(spec=Session)
        self.session.get_version.return_value = {}
        self.session.get_datetime.return_value = {}
        self.empty_signals = [
            {"dataId": signal, "events": [], "complete": True}
            for signal in (history.USAGE, history.RESPIRATORY, history.LEAK, history.PRESSURE)
        ]
        session_signals = [
            {**self.empty_signals[0], "events": [
                {"time": start.isoformat(), "event": "MaskOn"},
                {"time": self.end.isoformat(), "event": "MaskOff"},
            ]},
            {**self.empty_signals[1], "events": [
                {"time": (start + timedelta(hours=1)).isoformat(), "event": "ObstructiveApneaEnd"},
            ]},
            *self.empty_signals[2:],
        ]
        self.nightly_data = [{"method": "LoggedData", "params": {"data": session_signals}}]
        self.session.collect_logged_data.return_value = self.nightly_data

    async def read(self, status, *, last_used=None):
        self.session.get_settings.return_value = {"FGState": status}
        self.session.get_metrics.return_value = {
            "MachineMetrics": {"LastTherapyUseDateTime": last_used},
        }
        await self.controller._reads(self.session, self.address)

    def published_nights(self):
        return self.controller.publisher.publish.call_args.kwargs["nights"]

    async def test_downloads_and_publishes_at_standby_after_a_scheduled_therapy_read(self):
        self.manager.store.save(
            self.address, [self.prior], at=self.now - timedelta(hours=13)
        )
        self.session.collect_logged_data.side_effect = [
            [{"method": "LoggedData", "params": {"data": self.empty_signals}}],
            self.nightly_data,
        ]
        await self.read("Therapy")
        self.assertEqual([self.prior], self.published_nights())
        self.assertFalse(self.manager.is_due(self.address))
        await self.read("Standby")
        latest = self.published_nights()[0]
        self.assertEqual(self.night, latest["night"])
        self.assertEqual(4.0, latest["usage_hours"])
        self.assertEqual(0.25, latest["ahi"])
        await self.read("Standby")
        self.assertEqual(2, self.session.collect_logged_data.await_count)

    async def test_missing_status_preserves_the_transition_and_failure_retries(self):
        await self.read("Therapy")
        with self.assertLogs("airsupply", level="ERROR"):
            self.session.get_settings.side_effect = RuntimeError("read failed")
            await self.read(None)
        self.session.get_settings.side_effect = None
        self.session.collect_logged_data.side_effect = [TimeoutError(), self.nightly_data]
        with self.assertLogs("airsupply", level="WARNING"):
            await self.read("Standby")
        self.assertEqual([self.prior], self.published_nights())
        results = self.controller.readings[self.address]["results"]
        self.assertEqual("Standby", entities.state(results, None)["status"])
        await self.read("Standby")
        self.assertEqual(self.night, self.published_nights()[0]["night"])
        self.assertEqual(2, self.session.collect_logged_data.await_count)

    async def test_first_standby_read_after_restart_catches_up_from_last_use(self):
        self.manager.clock_offset = 120
        # The reported time is older than the last sync until the offset is applied.
        self.manager.store.save(self.address, [self.prior], at=self.end - timedelta(seconds=60))
        reported = (self.end - timedelta(seconds=120)).isoformat()
        await self.read("Standby", last_used=reported)
        self.assertEqual(4.0, self.published_nights()[0]["usage_hours"])
        await self.read("Standby", last_used=reported)
        self.session.collect_logged_data.assert_awaited_once()

    async def test_invalid_future_and_active_therapy_times_do_not_trigger_catch_up(self):
        await self.read("Standby", last_used="invalid")
        await self.read("Standby", last_used=(self.now + timedelta(hours=1)).isoformat())
        await self.read("Therapy", last_used=self.end.isoformat())
        self.session.collect_logged_data.assert_not_awaited()


class EntityTests(unittest.TestCase):
    def test_latest_night_uses_existing_mqtt_state_contract(self):
        latest = {
            "night": "2026-11-05", "usage_hours": 4.333,
            "ahi": 9.48, "obstructive_apnoeas": 39,
            "central_apnoeas": 0, "hypopnoeas": 2,
        }
        state = entities.state({}, "2026-11-16T00:00:00+00:00", latest_night=latest)
        self.assertEqual(9.48, state["night_ahi"])
        self.assertEqual(4.333, state["night_usage_hours"])
        self.assertNotIn("pressure_median_raw", state)
        self.assertIn(
            "homeassistant/sensor/airsupply_aa_bb/night_ahi/config",
            [topic for topic, _ in entities.discovery("AA:BB", "AirMini", None, None, "dev", set(state))]
        )


class DashboardDiscoveryTests(unittest.TestCase):
    def test_history_sensor_uses_nightly_mqtt_attributes(self):
        address = "AA:BB"
        doc = entities.state({}, "2026-11-16T00:00:00+00:00", history_count=14)
        self.assertEqual(14, doc["history_nights"])
        configurations = dict(entities.discovery(address, "AirMini", None, None,
                                                 "dev", set(doc)))
        history = configurations[
            "homeassistant/sensor/airsupply_aa_bb/history_nights/config"
        ]
        self.assertEqual("airsupply/aa_bb/nights", history["json_attributes_topic"])
        self.assertEqual("airsupply/aa_bb/state", history["state_topic"])
        self.assertEqual(
            "{{ value_json.history_nights }}", history["value_template"]
        )

    def test_history_does_not_affect_regular_sensor_when_absent(self):
        doc = entities.state({}, "2026-11-16T00:00:00+00:00")
        self.assertNotIn("history_nights", doc)
        self.assertIn("last_read", doc)


if __name__ == "__main__":
    unittest.main()
