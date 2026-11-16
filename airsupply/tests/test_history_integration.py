"""Tests for read-only history collection and persistent summaries.

No live Bluetooth connection, pairing keys or personal therapy data.
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from airsupply.airmini.session import Session
from airsupply.history_store import HistoryStore
from airsupply.history_sync import HistoryManager
from airsupply import entities


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
